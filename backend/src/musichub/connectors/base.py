"""Schnittstelle, die jeder Plattform-Connector erfüllt.

Regeln:
* Ein Connector kennt nur seine Plattform und liefert plattformneutrale DTOs
  (``RemoteTrack``, ``RemotePlaylist``). Er greift nie auf die Datenbank zu.
* Jeder Fehler wird als ``ConnectorError`` (oder Unterklasse) geworfen. Der Aufrufer
  geht immer über die ``ConnectorRegistry``, die Fehler pro Plattform isoliert.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from musichub.domain.types import Platform


class Capability(StrEnum):
    SEARCH = "search"
    READ_PLAYLISTS = "read_playlists"
    WRITE_PLAYLISTS = "write_playlists"
    ISRC_LOOKUP = "isrc_lookup"


@dataclass(frozen=True, slots=True)
class RemoteTrack:
    platform: Platform
    external_id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int | None = None
    isrc: str | None = None
    url: str | None = None


@dataclass(frozen=True, slots=True)
class RemotePlaylist:
    platform: Platform
    external_id: str
    name: str
    track_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class TrackQuery:
    title: str
    artist: str
    duration_ms: int | None = None
    isrc: str | None = None


class ConnectorError(Exception):
    """Fehler eines Connectors. ``retryable`` steuert, ob ein Job es erneut versucht."""

    def __init__(self, platform: Platform, message: str, *, retryable: bool = True) -> None:
        super().__init__(f"[{platform.value}] {message}")
        self.platform = platform
        self.message = message
        self.retryable = retryable


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
        )


class PlatformConnector(ABC):
    platform: Platform
    capabilities: frozenset[Capability] = frozenset()

    @abstractmethod
    def is_configured(self) -> bool:
        """True, wenn alle Zugangsdaten vorhanden sind. Ohne Netzwerkaufruf."""

    @abstractmethod
    def search_tracks(self, query: TrackQuery, limit: int = 10) -> Sequence[RemoteTrack]: ...

    @abstractmethod
    def get_track(self, external_id: str) -> RemoteTrack | None: ...

    @abstractmethod
    def get_playlist(self, external_id: str) -> RemotePlaylist: ...

    @abstractmethod
    def create_playlist(self, name: str, description: str | None = None) -> RemotePlaylist: ...

    @abstractmethod
    def replace_playlist_tracks(self, external_id: str, track_ids: Sequence[str]) -> None: ...
