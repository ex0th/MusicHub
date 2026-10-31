"""Standard-Connectoren für Spotify, YouTube und SoundCloud (alle in dieser Etappe lesend)."""

from __future__ import annotations

import httpx

from musichub.connectors.base import PlatformConnector
from musichub.connectors.soundcloud import SoundCloudConnector
from musichub.connectors.spotify import SpotifyConnector
from musichub.connectors.youtube import YouTubeConnector
from musichub.core.config import Settings

__all__ = ["SoundCloudConnector", "SpotifyConnector", "YouTubeConnector", "default_connectors"]


def default_connectors(
    settings: Settings, *, transport: httpx.BaseTransport | None = None
) -> list[PlatformConnector]:
    return [
        SpotifyConnector(settings, transport=transport),
        SoundCloudConnector(settings, transport=transport),
        YouTubeConnector(settings, transport=transport),
    ]
