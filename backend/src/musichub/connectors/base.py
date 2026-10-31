"""Schnittstelle, die jeder Plattform-Connector erfüllt.

Regeln:
* Ein Connector kennt nur seine Plattform und liefert plattformneutrale DTOs
  (``RemoteTrack``, ``RemotePlaylistInfo`` …). Er greift nie auf die Datenbank zu und
  speichert keine Tokens – Zugangsdaten bekommt er bei jedem Aufruf übergeben.
* Jeder Fehler wird als ``ConnectorError`` (oder Unterklasse) geworfen. Der Aufrufer
  geht immer über die ``ConnectorRegistry``, die Fehler pro Plattform isoliert.
* **Diese Etappe ist rein lesend:** Kein Connector verändert Playlists oder Bibliotheken
  auf der Plattform. Schreibende Methoden werfen ``ConnectorNotImplementedError``; der
  HTTP-Client der Connectoren lässt gegenüber den Plattform-APIs nur GET zu.
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

    @abstractmethod
    def is_configured(self) -> bool:
        """True, wenn alle Zugangsdaten vorhanden sind. Ohne Netzwerkaufruf."""

    def _unsupported(self, operation: str) -> ConnectorError:
        if not self.is_configured():
            return ConnectorNotConfiguredError(self.platform)
        return ConnectorNotImplementedError(self.platform, operation)

    # --- OAuth -------------------------------------------------------------------------

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
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

    def search_tracks(self, query: TrackQuery, limit: int = 10) -> Sequence[RemoteTrack]:
        raise self._unsupported("search_tracks")

    def get_track(self, external_id: str) -> RemoteTrack | None:
        raise self._unsupported("get_track")

    def get_playlist(self, external_id: str) -> RemotePlaylist:
        raise self._unsupported("get_playlist")

    # --- Schreiben: in dieser Etappe bewusst deaktiviert -------------------------------

    def create_playlist(self, name: str, description: str | None = None) -> RemotePlaylist:
        raise self._unsupported("create_playlist")

    def replace_playlist_tracks(self, external_id: str, track_ids: Sequence[str]) -> None:
        raise self._unsupported("replace_playlist_tracks")
