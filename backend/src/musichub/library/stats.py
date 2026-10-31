"""Kennzahlen für das Dashboard – in wenigen Abfragen, unabhängig von der Datenmenge."""

from __future__ import annotations

from sqlalchemy import Integer, func, select
from sqlalchemy.orm import Session

from musichub.api.schemas import LibraryStats, PlatformStats
from musichub.domain.types import Availability, LinkStatus, Platform
from musichub.library.queries import source_is_available
from musichub.models import Playlist, Tag, Track, TrackSource


def library_stats(db: Session) -> LibraryStats:
    tracks = db.scalar(select(func.count()).select_from(Track)) or 0
    playlists = db.scalar(select(func.count()).select_from(Playlist)) or 0
    tags = db.scalar(select(func.count()).select_from(Tag)) or 0

    # Pro Track: auf welchen Plattformen verfügbar?
    per_track = (
        select(
            TrackSource.track_id.label("track_id"),
            func.count(func.distinct(TrackSource.platform)).label("n"),
            func.min(TrackSource.platform).label("only"),
        )
        .where(source_is_available())
        .group_by(TrackSource.track_id)
        .subquery()
    )
    with_platform = db.scalar(select(func.count()).select_from(per_track)) or 0
    single = db.scalar(select(func.count()).select_from(per_track).where(per_track.c.n == 1)) or 0
    exclusive = dict(
        db.execute(
            select(per_track.c.only, func.count())
            .where(per_track.c.n == 1)
            .group_by(per_track.c.only)
        ).all()
    )

    available = dict(
        db.execute(
            select(TrackSource.platform, func.count(func.distinct(TrackSource.track_id)))
            .where(source_is_available())
            .group_by(TrackSource.platform)
        ).all()
    )
    unavailable = dict(
        db.execute(
            select(
                TrackSource.platform,
                func.sum(
                    (TrackSource.availability == Availability.UNAVAILABLE.value).cast(Integer)
                ),
            )
            .where(TrackSource.link_status == LinkStatus.LINKED.value)
            .group_by(TrackSource.platform)
        ).all()
    )

    return LibraryStats(
        tracks=tracks,
        playlists=playlists,
        tags=tags,
        local_only=tracks - with_platform,
        single_platform=single,
        platforms=[
            PlatformStats(
                platform=p,
                tracks=int(available.get(p.value, 0)),
                exclusive=int(exclusive.get(p.value, 0)),
                unavailable=int(unavailable.get(p.value) or 0),
            )
            for p in Platform
        ],
    )
