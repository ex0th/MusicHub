"""Tracks anlegen, ändern, löschen, suchen und filtern."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import ColumnElement, Select, delete, func, or_, select
from sqlalchemy.orm import Session

from musichub.api.schemas import SortOrder, TrackCreate, TrackSort, TrackUpdate
from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.domain.types import Platform
from musichub.library import playlists as playlist_service
from musichub.library import sources as source_service
from musichub.library.queries import available_on, available_platform_count
from musichub.models import Playlist, PlaylistItem, Tag, Track, track_tags


@dataclass(frozen=True, slots=True)
class TrackFilter:
    #: Volltext über Titel, Interpret, Album und Version (Teilstring, ohne Groß/Klein)
    q: str | None = None
    #: muss auf allen genannten Plattformen verfügbar sein
    platforms: Sequence[Platform] = field(default_factory=tuple)
    #: darf auf keiner der genannten Plattformen verfügbar sein
    missing_platforms: Sequence[Platform] = field(default_factory=tuple)
    #: genaue Anzahl Plattformen mit verfügbarer Quelle (0 = nur lokal)
    platform_count: int | None = None
    #: muss alle genannten Tags haben
    tag_ids: Sequence[uuid.UUID] = field(default_factory=tuple)
    has_isrc: bool | None = None


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _apply_filter(stmt: Select[tuple[Track]], f: TrackFilter) -> Select[tuple[Track]]:
    if f.q:
        conditions: list[ColumnElement[bool]] = []
        for term in f.q.split():
            pattern = _like(term)
            conditions.append(
                or_(
                    Track.title.ilike(pattern, escape="\\"),
                    Track.artist.ilike(pattern, escape="\\"),
                    Track.album.ilike(pattern, escape="\\"),
                    Track.version.ilike(pattern, escape="\\"),
                    Track.isrc.ilike(pattern, escape="\\"),
                )
            )
        stmt = stmt.where(*conditions)  # jedes Wort muss irgendwo vorkommen
    for platform in f.platforms:
        stmt = stmt.where(available_on(platform))
    for platform in f.missing_platforms:
        stmt = stmt.where(~available_on(platform))
    if f.platform_count is not None:
        stmt = stmt.where(available_platform_count() == f.platform_count)
    for tag_id in f.tag_ids:
        stmt = stmt.where(
            select(track_tags.c.track_id)
            .where(track_tags.c.track_id == Track.id, track_tags.c.tag_id == tag_id)
            .exists()
        )
    if f.has_isrc is not None:
        stmt = stmt.where(Track.isrc.is_not(None) if f.has_isrc else Track.isrc.is_(None))
    return stmt


_SORT_COLUMNS = {
    TrackSort.TITLE: Track.title,
    TrackSort.ARTIST: Track.artist,
    TrackSort.ALBUM: Track.album,
    TrackSort.VERSION: Track.version,
    TrackSort.DURATION: Track.duration_ms,
    TrackSort.RELEASE_DATE: Track.release_date,
    TrackSort.CREATED_AT: Track.created_at,
    TrackSort.UPDATED_AT: Track.updated_at,
}


def list_tracks(
    db: Session,
    f: TrackFilter,
    *,
    sort: TrackSort = TrackSort.ARTIST,
    order: SortOrder = SortOrder.ASC,
    limit: int = 50,
    offset: int = 0,
) -> tuple[Sequence[Track], int]:
    filtered = _apply_filter(select(Track), f)
    total = db.scalar(select(func.count()).select_from(filtered.order_by(None).subquery())) or 0

    column = _SORT_COLUMNS[sort]
    # Texte ohne Groß/Klein sortieren, leere Werte immer ans Ende
    key = (
        func.lower(column)
        if sort in {TrackSort.TITLE, TrackSort.ARTIST, TrackSort.ALBUM, TrackSort.VERSION}
        else column
    )
    primary = key.desc().nulls_last() if order is SortOrder.DESC else key.asc().nulls_last()
    stmt = (
        filtered.order_by(primary, func.lower(Track.artist), func.lower(Track.title), Track.id)
        .limit(limit)
        .offset(offset)
    )
    return db.scalars(stmt).all(), total


def get_track(db: Session, track_id: uuid.UUID) -> Track:
    track = db.get(Track, track_id)
    if track is None:
        raise NotFoundError(f"Track {track_id} nicht gefunden.")
    return track


def load_tags(db: Session, tag_ids: Sequence[uuid.UUID]) -> list[Tag]:
    unique_ids = list(dict.fromkeys(tag_ids))
    if not unique_ids:
        return []
    tags = db.scalars(select(Tag).where(Tag.id.in_(unique_ids))).all()
    missing = set(unique_ids) - {t.id for t in tags}
    if missing:
        raise UnprocessableError(
            "Unbekannte Tags.", extra={"missing_tag_ids": sorted(str(m) for m in missing)}
        )
    return list(tags)


def create_track(db: Session, data: TrackCreate) -> Track:
    fields = data.model_dump(exclude={"tag_ids", "sources"})
    track = Track(**fields)
    track.tags = load_tags(db, data.tag_ids)
    db.add(track)
    db.flush()
    for source in data.sources:
        source_service.add_source(db, track, source)
    return track


def update_track(db: Session, track: Track, data: TrackUpdate) -> Track:
    changes = data.model_dump(exclude_unset=True, exclude={"tag_ids"})
    for name, value in changes.items():
        setattr(track, name, value)
    if data.tag_ids is not None:
        track.tags = load_tags(db, data.tag_ids)
        track.updated_at = func.now()
    db.flush()
    return track


def set_tags(db: Session, track: Track, tag_ids: Sequence[uuid.UUID]) -> Track:
    track.tags = load_tags(db, tag_ids)
    track.updated_at = func.now()
    db.flush()
    return track


def playlists_of(db: Session, track_id: uuid.UUID) -> list[tuple[uuid.UUID, str, int]]:
    rows = db.execute(
        select(Playlist.id, Playlist.name, func.count(PlaylistItem.id))
        .join(PlaylistItem, PlaylistItem.playlist_id == Playlist.id)
        .where(PlaylistItem.track_id == track_id)
        .group_by(Playlist.id, Playlist.name)
        .order_by(func.lower(Playlist.name))
    ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def delete_track(db: Session, track_id: uuid.UUID, *, force: bool = False) -> None:
    """Löscht einen Track. Steckt er in Playlists, nur mit ``force`` – dann werden die
    Einträge entfernt und die Positionen der betroffenen Playlists neu durchnummeriert."""
    track = get_track(db, track_id)
    affected = playlists_of(db, track_id)
    if affected and not force:
        raise ConflictError(
            f"Track ist in {len(affected)} Playlist(s) enthalten. "
            "Zum Löschen inklusive aller Playlist-Einträge force=true setzen.",
            extra={"playlists": [{"id": str(p[0]), "name": p[1]} for p in affected]},
        )
    for playlist_id, _, _ in affected:
        playlist = playlist_service.lock_playlist(db, playlist_id)
        db.execute(
            delete(PlaylistItem).where(
                PlaylistItem.playlist_id == playlist_id, PlaylistItem.track_id == track_id
            )
        )
        playlist_service.renumber(db, playlist)
    db.delete(track)
    db.flush()
