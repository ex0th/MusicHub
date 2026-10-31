"""Playlists und ihre Reihenfolge.

Invariante: Die Positionen einer Playlist sind lückenlos 0..n-1. Jede Änderung sperrt
zuerst die Playlist-Zeile (``FOR UPDATE``), damit gleichzeitige Änderungen sich nicht
überschneiden. Die Eindeutigkeit (playlist_id, position) prüft PostgreSQL erst beim
Commit, daher sind Verschiebungen per UPDATE ohne Zwischenschritte möglich.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import case, delete, func, select, update
from sqlalchemy.orm import Session

from musichub.api.schemas import PlaylistCreate, PlaylistUpdate
from musichub.core.errors import NotFoundError, UnprocessableError
from musichub.models import Playlist, PlaylistItem, Track


def lock_playlist(db: Session, playlist_id: uuid.UUID) -> Playlist:
    playlist = db.scalar(
        select(Playlist).where(Playlist.id == playlist_id).with_for_update(of=Playlist)
    )
    if playlist is None:
        raise NotFoundError(f"Playlist {playlist_id} nicht gefunden.")
    return playlist


def get_playlist(db: Session, playlist_id: uuid.UUID) -> Playlist:
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        raise NotFoundError(f"Playlist {playlist_id} nicht gefunden.")
    return playlist


def _touch(playlist: Playlist) -> None:
    playlist.updated_at = func.now()


def _count(db: Session, playlist_id: uuid.UUID) -> int:
    return db.scalar(select(func.count()).where(PlaylistItem.playlist_id == playlist_id)) or 0


def renumber(db: Session, playlist: Playlist) -> None:
    """Positionen nach Löschungen wieder auf 0..n-1 bringen (Reihenfolge bleibt)."""
    numbered = (
        select(
            PlaylistItem.id,
            (
                func.row_number().over(order_by=(PlaylistItem.position, PlaylistItem.added_at)) - 1
            ).label("rn"),
        )
        .where(PlaylistItem.playlist_id == playlist.id)
        .subquery()
    )
    db.execute(
        update(PlaylistItem)
        .where(PlaylistItem.id == numbered.c.id, PlaylistItem.position != numbered.c.rn)
        .values(position=numbered.c.rn)
    )
    _touch(playlist)


# --- Lesen ---------------------------------------------------------------------


def list_playlists(
    db: Session, *, q: str | None = None, limit: int = 50, offset: int = 0
) -> tuple[list[tuple[Playlist, int, int]], int]:
    stats = (
        select(
            PlaylistItem.playlist_id.label("pid"),
            func.count(PlaylistItem.id).label("n"),
            func.coalesce(func.sum(Track.duration_ms), 0).label("ms"),
        )
        .join(Track, Track.id == PlaylistItem.track_id)
        .group_by(PlaylistItem.playlist_id)
        .subquery()
    )
    stmt = select(Playlist, func.coalesce(stats.c.n, 0), func.coalesce(stats.c.ms, 0)).outerjoin(
        stats, stats.c.pid == Playlist.id
    )
    if q:
        stmt = stmt.where(Playlist.name.ilike(f"%{q}%"))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.execute(
        stmt.order_by(func.lower(Playlist.name), Playlist.id).limit(limit).offset(offset)
    ).all()
    return [(r[0], int(r[1]), int(r[2])) for r in rows], total


def items_of(db: Session, playlist_id: uuid.UUID) -> Sequence[PlaylistItem]:
    return db.scalars(
        select(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist_id)
        .order_by(PlaylistItem.position)
    ).all()


# --- Playlist selbst -----------------------------------------------------------


def create_playlist(db: Session, data: PlaylistCreate) -> Playlist:
    playlist = Playlist(name=data.name, description=data.description)
    db.add(playlist)
    db.flush()
    if data.track_ids:
        add_items(db, playlist.id, data.track_ids)
    return playlist


def update_playlist(db: Session, playlist: Playlist, data: PlaylistUpdate) -> Playlist:
    for name, value in data.model_dump(exclude_unset=True).items():
        setattr(playlist, name, value)
    db.flush()
    return playlist


def delete_playlist(db: Session, playlist: Playlist) -> None:
    db.delete(playlist)  # Einträge per ON DELETE CASCADE / ORM-Cascade
    db.flush()


# --- Einträge ------------------------------------------------------------------


def add_items(
    db: Session,
    playlist_id: uuid.UUID,
    track_ids: Sequence[uuid.UUID],
    *,
    position: int | None = None,
    skip_existing: bool = False,
) -> list[PlaylistItem]:
    playlist = lock_playlist(db, playlist_id)

    known = set(db.scalars(select(Track.id).where(Track.id.in_(set(track_ids)))).all())
    missing = [str(t) for t in dict.fromkeys(track_ids) if t not in known]
    if missing:
        raise UnprocessableError("Unbekannte Tracks.", extra={"missing_track_ids": missing})

    to_add = list(track_ids)
    if skip_existing:
        present = set(
            db.scalars(
                select(PlaylistItem.track_id).where(PlaylistItem.playlist_id == playlist_id)
            ).all()
        )
        to_add = [t for t in dict.fromkeys(to_add) if t not in present]
    if not to_add:
        return []

    count = _count(db, playlist_id)
    start = count if position is None else min(position, count)
    if start < count:
        db.execute(
            update(PlaylistItem)
            .where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.position >= start)
            .values(position=PlaylistItem.position + len(to_add))
        )
    items = [
        PlaylistItem(playlist_id=playlist_id, track_id=track_id, position=start + i)
        for i, track_id in enumerate(to_add)
    ]
    db.add_all(items)
    _touch(playlist)
    db.flush()
    return items


def get_item(db: Session, playlist_id: uuid.UUID, item_id: uuid.UUID) -> PlaylistItem:
    item = db.get(PlaylistItem, item_id)
    if item is None or item.playlist_id != playlist_id:
        raise NotFoundError(f"Playlist-Eintrag {item_id} nicht gefunden.")
    return item


def remove_item(db: Session, playlist_id: uuid.UUID, item_id: uuid.UUID) -> None:
    playlist = lock_playlist(db, playlist_id)
    item = get_item(db, playlist_id, item_id)
    removed_at = item.position
    db.execute(delete(PlaylistItem).where(PlaylistItem.id == item.id))
    db.execute(
        update(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist_id, PlaylistItem.position > removed_at)
        .values(position=PlaylistItem.position - 1)
    )
    _touch(playlist)
    db.flush()
    db.expire_all()


def move_item(db: Session, playlist_id: uuid.UUID, item_id: uuid.UUID, position: int) -> None:
    playlist = lock_playlist(db, playlist_id)
    item = get_item(db, playlist_id, item_id)
    target = min(position, _count(db, playlist_id) - 1)
    source = item.position
    if target == source:
        return
    if target > source:  # nach hinten: dazwischenliegende rücken auf
        shift = (
            (PlaylistItem.position > source) & (PlaylistItem.position <= target),
            PlaylistItem.position - 1,
        )
    else:  # nach vorn: dazwischenliegende rücken nach hinten
        shift = (
            (PlaylistItem.position >= target) & (PlaylistItem.position < source),
            PlaylistItem.position + 1,
        )
    db.execute(
        update(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist_id)
        .values(
            position=case(
                (PlaylistItem.id == item.id, target),
                shift,
                else_=PlaylistItem.position,
            )
        )
    )
    _touch(playlist)
    db.flush()
    db.expire_all()


def reorder(db: Session, playlist_id: uuid.UUID, item_ids: Sequence[uuid.UUID]) -> None:
    playlist = lock_playlist(db, playlist_id)
    current = set(
        db.scalars(select(PlaylistItem.id).where(PlaylistItem.playlist_id == playlist_id)).all()
    )
    if len(item_ids) != len(set(item_ids)) or set(item_ids) != current:
        raise UnprocessableError(
            "item_ids muss jeden Eintrag der Playlist genau einmal enthalten.",
            extra={"expected_count": len(current)},
        )
    if not item_ids:
        return
    db.execute(
        update(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist_id)
        .values(
            position=case(
                {item_id: index for index, item_id in enumerate(item_ids)},
                value=PlaylistItem.id,
            )
        )
    )
    _touch(playlist)
    db.flush()
    db.expire_all()
