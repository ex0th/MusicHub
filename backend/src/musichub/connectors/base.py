"""Schnittstelle, die jeder Plattform-Connector erfüllt.

Regeln:
* Ein Connector kennt nur seine Plattform und liefert plattformneutrale DTOs
  (``RemoteTrack``, ``RemotePlaylistInfo`` …). Er greift nie auf die Datenbank zu und
  speichert keine Tokens – Zugangsdaten bekommt er bei jedem Aufruf übergeben.
* Jeder Fehler wird als ``ConnectorError`` (oder Unterklasse) geworfen. Der Aufrufer
  geht immer über die ``ConnectorRegistry``, die Fehler pro Plattform isoliert.
* Schreiben auf Plattformen gibt es nur für die Synchronisierung (MusicHub → Plattform):
  nur Playlist-Inhalte, nie Bibliotheken/Likes, nur mit Schreib-Scopes, die der Nutzer beim
  Verbinden ausdrücklich gewährt hat, und nur über ``sync/executor.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from musichub.domain.types import Platform


class Capability(StrEnum):
    SEARCH = "search"
    READ_PLAYLISTS = "read_playlists"
    WRITE_PLAYLISTS = "write_playlists"
    ISRC_LOOKUP = "isrc_lookup"
    #: Anmeldung eines Nutzerkontos per OAuth
    OAUTH = "oauth"
    #: Metadaten bereits verknüpfter Tracks nachladen
    REFRESH_METADATA = "refresh_metadata"


@dataclass(frozen=True, slots=True)
class RemoteTrack:
    platform: Platform
    external_id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int | None = None
    isrc: str | None = None
    url: str | None = None
    album: str | None = None
    #: ISO-Datum (nur bei tagesgenauer Angabe der Plattform)
    release_date: str | None = None
    #: False, wenn die Plattform den Inhalt als gelöscht/privat/gesperrt meldet
    available: bool = True
    #: Hinweis für das Importprotokoll (z. B. „lokale Datei“, „privates Video“)
    note: str | None = None
    #: ID des Playlist-Eintrags (YouTube: playlistItem-ID), nur in Playlist-Inhalten
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class RemotePlaylist:
    platform: Platform
    external_id: str
    name: str
    track_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class RemotePlaylistInfo:
    """Eine Playlist des verbundenen Kontos (nur Metadaten)."""

    platform: Platform
    external_id: str
    name: str
    track_count: int | None
    url: str | None = None
    owner_name: str | None = None
    description: str | None = None
    #: gehört dem verbundenen Konto
    owned: bool = True
    #: Inhalte (Tracks) dürfen gelesen werden – bei Spotify im Development Mode nur bei
    #: eigenen oder kollaborativen Playlists
    items_accessible: bool = True
    #: Versionsmerkmal für inkrementelle Aktualisierung (snapshot_id, ETag, last_modified)
    version: str | None = None


@dataclass(frozen=True, slots=True)
class TrackPage:
    tracks: list[RemoteTrack]
    #: Gesamtzahl laut Plattform (für Fortschritt), falls bekannt
    total: int | None = None


@dataclass(frozen=True, slots=True)
class RemoteAccount:
    external_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class OAuthTokens:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str | None = None

    def __repr__(self) -> str:  # Tokens nie in Logs/Tracebacks
        return f"OAuthTokens(expires_at={self.expires_at!r}, scope={self.scope!r})"


@dataclass(frozen=True, slots=True)
class TrackQuery:
    title: str
    artist: str
    duration_ms: int | None = None
    isrc: str | None = None


class ConnectorError(Exception):
    """Fehler eines Connectors. ``retryable`` steuert, ob ein Job es erneut versucht;
    ``retry_after`` (Sekunden) überschreibt das normale Backoff, z. B. bei Rate-Limits."""

    def __init__(
        self,
        platform: Platform,
        message: str,
        *,
        retryable: bool = True,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(f"[{platform.value}] {message}")
        self.platform = platform
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


class ConnectorNotConfiguredError(ConnectorError):
    def __init__(self, platform: Platform) -> None:
        super().__init__(platform, "Connector ist nicht konfiguriert.", retryable=False)


class ConnectorNotImplementedError(ConnectorError):
    def __init__(self, platform: Platform, operation: str) -> None:
        super().__init__(
            platform, f"Operation '{operation}' ist noch nicht implementiert.", retryable=False
        )


class ConnectorUnavailableError(ConnectorError):
    """Circuit Breaker offen: Plattform wird vorübergehend nicht angesprochen."""

    def __init__(self, platform: Platform, retry_in_seconds: float) -> None:
        super().__init__(
            platform,
            "Vorübergehend deaktiviert nach wiederholten Fehlern "
            f"(erneut in {retry_in_seconds:.0f} s).",
            retryable=True,
            retry_after=retry_in_seconds,
        )


class RateLimitedError(ConnectorError):
    """HTTP 429 oder erschöpftes Tageskontingent – später erneut versuchen."""

    def __init__(self, platform: Platform, retry_after: float, message: str | None = None) -> None:
        super().__init__(
            platform,
            message or f"Rate-Limit erreicht, erneut in {retry_after:.0f} s.",
            retryable=True,
            retry_after=retry_after,
        )


class ConnectorAuthError(ConnectorError):
    """Token ungültig, abgelaufen oder widerrufen – das Konto muss neu verbunden werden."""

    def __init__(
        self, platform: Platform, message: str = "Anmeldung abgelaufen oder widerrufen."
    ) -> None:
        super().__init__(platform, message, retryable=False)


class ConnectorAccessError(ConnectorError):
    """Die Plattform verweigert den Zugriff auf eine Ressource (403/404) – nicht wiederholbar."""

    def __init__(self, platform: Platform, message: str, *, status: int) -> None:
        super().__init__(platform, message, retryable=False)
        self.status = status


class PlatformConnector(ABC):
    """Basis aller Connectoren. Nicht unterstützte Operationen werfen
    ``ConnectorNotImplementedError`` (nicht wiederholbar)."""

    platform: Platform
    capabilities: frozenset[Capability] = frozenset()
    #: angeforderte OAuth-Scopes (so wenige wie möglich)
    scopes: tuple[str, ...] = ()
    #: Scopes mit Schreibzugriff – nur auf ausdrücklichen Wunsch (Synchronisierung)
    write_scopes: tuple[str, ...] = ()
    #: Zielplaylist kann in einem Schritt vollständig ersetzt werden (Spotify)
    can_replace: bool = False
    #: Kontingent-Einheiten je schreibendem Aufruf (YouTube), für die Vorschau
    write_cost_units: int | None = None

    def has_write_scope(self, granted: str | None) -> bool:
        """Wurden die Schreib-Scopes tatsächlich gewährt?"""
        have = set((granted or "").split())
        needed = set(self.write_scopes) - set(self.scopes)
        return bool(needed) and needed <= have

    @abstractmethod
    def is_configured(self) -> bool:
        """True, wenn alle Zugangsdaten vorhanden sind. Ohne Netzwerkaufruf."""

    def _unsupported(self, operation: str) -> ConnectorError:
        if not self.is_configured():
            return ConnectorNotConfiguredError(self.platform)
        return ConnectorNotImplementedError(self.platform, operation)

    # --- OAuth -------------------------------------------------------------------------

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str, write: bool = False
    ) -> str:
        raise self._unsupported("authorization_url")

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        raise self._unsupported("exchange_code")

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        raise self._unsupported("refresh_tokens")

    def revoke(self, tokens: OAuthTokens) -> bool:
        """Widerruft die Berechtigung bei der Plattform, falls sie das anbietet."""
        return False

    # --- Lesen ---------------------------------------------------------------------------

    def get_account(self, access_token: str) -> RemoteAccount:
        raise self._unsupported("get_account")

    def list_playlists(self, access_token: str, account: RemoteAccount) -> list[RemotePlaylistInfo]:
        raise self._unsupported("list_playlists")

    def iter_playlist_tracks(self, access_token: str, playlist_id: str) -> Iterator[TrackPage]:
        raise self._unsupported("iter_playlist_tracks")

    def get_tracks(
        self, access_token: str, external_ids: Sequence[str]
    ) -> dict[str, RemoteTrack | None]:
        """Aktuelle Metadaten; ``None`` = auf der Plattform nicht (mehr) vorhanden."""
        raise self._unsupported("get_tracks")

    def search(self, access_token: str, query: TrackQuery, limit: int = 5) -> list[RemoteTrack]:
        """Kandidaten für einen lokalen Track (ohne Bewertung – die macht die Matching-Engine)."""
        raise self._unsupported("search")

    # --- Schreiben: nur für die Synchronisierung (sync/executor.py) -----------------------

    def create_remote_playlist(
        self, access_token: str, name: str, description: str | None
    ) -> RemotePlaylistInfo:
        """Legt eine **private** Playlist an (wird dadurch von MusicHub verwaltet)."""
        raise self._unsupported("create_remote_playlist")

    def add_to_playlist(
        self, access_token: str, playlist_id: str, external_ids: Sequence[str]
    ) -> None:
        """Hängt Tracks in der angegebenen Reihenfolge an."""
        raise self._unsupported("add_to_playlist")

    def replace_playlist(
        self, access_token: str, playlist_id: str, external_ids: Sequence[str]
    ) -> None:
        """Ersetzt den kompletten Inhalt (nur wenn ``can_replace``)."""
        raise self._unsupported("replace_playlist")

    def remove_from_playlist(
        self, access_token: str, playlist_id: str, item_ids: Sequence[str]
    ) -> None:
        """Entfernt einzelne Einträge über ihre Eintrags-ID (YouTube)."""
        raise self._unsupported("remove_from_playlist")

    def move_in_playlist(
        self, access_token: str, playlist_id: str, item_id: str, external_id: str, position: int
    ) -> None:
        raise self._unsupported("move_in_playlist")
