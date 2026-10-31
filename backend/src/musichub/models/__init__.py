"""SQLAlchemy-Modelle. Alle Modelle müssen hier importiert werden, damit Alembic sie sieht."""

from musichub.db.base import Base
from musichub.models.connections import ConnectorAccount, OAuthState, RemotePlaylist
from musichub.models.imports import ImportItem, ImportRun
from musichub.models.job import Job
from musichub.models.library import (
    Playlist,
    PlaylistItem,
    PlaylistTarget,
    Tag,
    Track,
    TrackSource,
    track_tags,
)
from musichub.models.matching import MatchCandidate, TrackMerge

__all__ = [
    "Base",
    "ConnectorAccount",
    "ImportItem",
    "ImportRun",
    "Job",
    "MatchCandidate",
    "OAuthState",
    "Playlist",
    "PlaylistItem",
    "PlaylistTarget",
    "RemotePlaylist",
    "Tag",
    "Track",
    "TrackMerge",
    "TrackSource",
    "track_tags",
]
