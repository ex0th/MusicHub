"""Connectoren für Spotify, SoundCloud und YouTube.

Stand Etappe 1: Konfigurationserkennung und Schnittstelle stehen, die API-Aufrufe
folgen in späteren Etappen (siehe docs/ROADMAP.md). Nicht implementierte Operationen
werfen ``ConnectorNotImplementedError`` – das ist ein sauberer, nicht-wiederholbarer
Fehler, den Registry und Jobs korrekt behandeln.
"""

from __future__ import annotations

from collections.abc import Sequence

from musichub.connectors.base import (
    Capability,
    ConnectorNotConfiguredError,
    ConnectorNotImplementedError,
    PlatformConnector,
    RemotePlaylist,
    RemoteTrack,
    TrackQuery,
)
from musichub.core.config import Settings
from musichub.domain.types import Platform


class _NotYetImplementedConnector(PlatformConnector):
    def _guard(self, operation: str) -> None:
        if not self.is_configured():
            raise ConnectorNotConfiguredError(self.platform)
        raise ConnectorNotImplementedError(self.platform, operation)

    def search_tracks(self, query: TrackQuery, limit: int = 10) -> Sequence[RemoteTrack]:
        self._guard("search_tracks")
        raise AssertionError("unreachable")

    def get_track(self, external_id: str) -> RemoteTrack | None:
        self._guard("get_track")
        raise AssertionError("unreachable")

    def get_playlist(self, external_id: str) -> RemotePlaylist:
        self._guard("get_playlist")
        raise AssertionError("unreachable")

    def create_playlist(self, name: str, description: str | None = None) -> RemotePlaylist:
        self._guard("create_playlist")
        raise AssertionError("unreachable")

    def replace_playlist_tracks(self, external_id: str, track_ids: Sequence[str]) -> None:
        self._guard("replace_playlist_tracks")


class SpotifyConnector(_NotYetImplementedConnector):
    platform = Platform.SPOTIFY
    capabilities = frozenset(
        {
            Capability.SEARCH,
            Capability.READ_PLAYLISTS,
            Capability.WRITE_PLAYLISTS,
            Capability.ISRC_LOOKUP,
        }
    )

    def __init__(self, settings: Settings) -> None:
        self._configured = bool(settings.spotify_client_id and settings.spotify_client_secret)

    def is_configured(self) -> bool:
        return self._configured


class SoundCloudConnector(_NotYetImplementedConnector):
    platform = Platform.SOUNDCLOUD
    capabilities = frozenset(
        {Capability.SEARCH, Capability.READ_PLAYLISTS, Capability.WRITE_PLAYLISTS}
    )

    def __init__(self, settings: Settings) -> None:
        self._configured = bool(settings.soundcloud_client_id and settings.soundcloud_client_secret)

    def is_configured(self) -> bool:
        return self._configured


class YouTubeConnector(_NotYetImplementedConnector):
    platform = Platform.YOUTUBE
    # Schreiben von Playlists erfordert OAuth (nicht nur API-Key) – folgt mit Connector-Accounts.
    capabilities = frozenset(
        {Capability.SEARCH, Capability.READ_PLAYLISTS, Capability.WRITE_PLAYLISTS}
    )

    def __init__(self, settings: Settings) -> None:
        self._configured = settings.youtube_api_key is not None

    def is_configured(self) -> bool:
        return self._configured


def default_connectors(settings: Settings) -> list[PlatformConnector]:
    return [SpotifyConnector(settings), SoundCloudConnector(settings), YouTubeConnector(settings)]
