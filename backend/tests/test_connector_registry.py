"""Fehlerisolation: Ein kaputter Connector darf andere Plattformen nicht beeinträchtigen."""

from collections.abc import Sequence

import pytest

from musichub.connectors.base import (
    ConnectorError,
    ConnectorNotConfiguredError,
    ConnectorNotImplementedError,
    ConnectorUnavailableError,
    PlatformConnector,
    RemotePlaylist,
    RemoteTrack,
    TrackQuery,
)
from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry, ConnectorState
from musichub.core.config import get_settings
from musichub.domain.types import Platform


class FakeConnector(PlatformConnector):
    def __init__(self, platform: Platform, *, fail_with: Exception | None = None) -> None:
        self.platform = platform
        self.fail_with = fail_with

    def is_configured(self) -> bool:
        return True

    def search_tracks(self, query: TrackQuery, limit: int = 10) -> Sequence[RemoteTrack]:
        if self.fail_with:
            raise self.fail_with
        return [RemoteTrack(self.platform, "1", query.title, (query.artist,))]

    def get_track(self, external_id: str) -> RemoteTrack | None:
        return None

    def get_playlist(self, external_id: str) -> RemotePlaylist:
        return RemotePlaylist(self.platform, external_id, "x")

    def create_playlist(self, name: str, description: str | None = None) -> RemotePlaylist:
        return RemotePlaylist(self.platform, "new", name)

    def replace_playlist_tracks(self, external_id: str, track_ids: Sequence[str]) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


Q = TrackQuery(title="Song", artist="Artist")


def test_foreign_exception_is_wrapped_and_isolated() -> None:
    broken = FakeConnector(Platform.SOUNDCLOUD, fail_with=KeyError("tracks"))
    healthy = FakeConnector(Platform.SPOTIFY)
    registry = ConnectorRegistry([broken, healthy])

    with pytest.raises(ConnectorError) as exc_info:
        registry.call(Platform.SOUNDCLOUD, lambda c: c.search_tracks(Q))
    assert exc_info.value.platform is Platform.SOUNDCLOUD
    assert exc_info.value.retryable

    result = registry.call(Platform.SPOTIFY, lambda c: c.search_tracks(Q))
    assert result[0].title == "Song"

    states = {s.platform: s.state for s in registry.status()}
    assert states == {
        Platform.SOUNDCLOUD: ConnectorState.DEGRADED,
        Platform.SPOTIFY: ConnectorState.READY,
    }


def test_circuit_breaker_opens_and_recovers() -> None:
    clock = Clock()
    broken = FakeConnector(Platform.YOUTUBE, fail_with=TimeoutError("slow"))
    registry = ConnectorRegistry([broken], failure_threshold=2, cooldown_seconds=60, clock=clock)

    for _ in range(2):
        with pytest.raises(ConnectorError):
            registry.call(Platform.YOUTUBE, lambda c: c.search_tracks(Q))

    with pytest.raises(ConnectorUnavailableError):
        registry.call(Platform.YOUTUBE, lambda c: c.search_tracks(Q))
    assert registry.status()[0].state is ConnectorState.SUSPENDED

    clock.now += 61
    broken.fail_with = None
    assert registry.call(Platform.YOUTUBE, lambda c: c.search_tracks(Q))
    assert registry.status()[0].state is ConnectorState.READY


def test_non_retryable_errors_do_not_trip_breaker() -> None:
    connector = FakeConnector(
        Platform.SPOTIFY, fail_with=ConnectorNotImplementedError(Platform.SPOTIFY, "x")
    )
    registry = ConnectorRegistry([connector], failure_threshold=1)
    with pytest.raises(ConnectorNotImplementedError):
        registry.call(Platform.SPOTIFY, lambda c: c.search_tracks(Q))
    assert registry.status()[0].state is ConnectorState.READY


def test_unconfigured_default_connectors() -> None:
    registry = ConnectorRegistry(default_connectors(get_settings()))
    assert {s.state for s in registry.status()} == {ConnectorState.NOT_CONFIGURED}
    with pytest.raises(ConnectorNotConfiguredError):
        registry.call(Platform.SPOTIFY, lambda c: c.search_tracks(Q))
