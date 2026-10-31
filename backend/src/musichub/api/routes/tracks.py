"""Tracks der lokalen Sammlung. Lesen: angemeldet. Schreiben: ``require_write``."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from musichub.api.schemas import Page, TrackCreate, TrackOut, TrackUpdate
from musichub.auth import Principal, current_principal, require_write
from musichub.core.errors import ConflictError, NotFoundError
from musichub.db.session import get_db
from musichub.models import PlaylistItem, Track

router = APIRouter(prefix="/api/v1/tracks", tags=["tracks"])


def _get_or_404(db: Session, track_id: uuid.UUID) -> Track:
    track = db.get(Track, track_id)
    if track is None:
        raise NotFoundError(f"Track {track_id} nicht gefunden.")
    return track


@router.get("", response_model=Page[TrackOut], summary="Tracks auflisten/suchen")
def list_tracks(
    q: str | None = Query(None, max_length=200, description="Suche in Titel, Interpret, Album"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[TrackOut]:
    stmt = select(Track)
    if q:
        pattern = f"%{q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')}%"
        stmt = stmt.where(
            or_(
                Track.title.ilike(pattern, escape="\\"),
                Track.artist.ilike(pattern, escape="\\"),
                Track.album.ilike(pattern, escape="\\"),
            )
        )
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(stmt.order_by(Track.artist, Track.title).limit(limit).offset(offset)).all()
    return Page[TrackOut](
        items=[TrackOut.model_validate(t) for t in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{track_id}", response_model=TrackOut)
def get_track(
    track_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> Track:
    return _get_or_404(db, track_id)


@router.post("", response_model=TrackOut, status_code=201, summary="Track anlegen")
def create_track(
    body: TrackCreate, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> Track:
    track = Track(**body.model_dump())
    db.add(track)
    db.commit()
    db.refresh(track)
    return track


@router.patch("/{track_id}", response_model=TrackOut, summary="Track ändern")
def update_track(
    track_id: uuid.UUID,
    body: TrackUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> Track:
    track = _get_or_404(db, track_id)
    for field, value in body.model_dump(exclude_unset=True).items():
        if field in {"title", "artist"} and value is None:
            continue  # Pflichtfelder können nicht geleert werden
        setattr(track, field, value)
    db.commit()
    db.refresh(track)
    return track


@router.delete("/{track_id}", status_code=204, summary="Track löschen")
def delete_track(
    track_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> Response:
    track = _get_or_404(db, track_id)
    in_use = db.scalar(select(func.count()).where(PlaylistItem.track_id == track_id)) or 0
    if in_use:
        raise ConflictError(f"Track ist in {in_use} Playlist-Einträgen enthalten.")
    db.delete(track)
    try:
        db.commit()
    except IntegrityError as exc:  # Race mit gleichzeitigem Playlist-Insert
        db.rollback()
        raise ConflictError("Track wird noch verwendet.") from exc
    return Response(status_code=204)
