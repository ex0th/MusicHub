"""Tracks, ihre Quellen und Tags. Lesen: angemeldet. Schreiben: ``require_write``."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from musichub.api.schemas import (
    Page,
    PlaylistRef,
    SortOrder,
    SourceCreate,
    SourceOut,
    SourceUpdate,
    TagAssignment,
    TrackCreate,
    TrackDetailOut,
    TrackOut,
    TrackSort,
    TrackUpdate,
)
from musichub.auth import Principal, current_principal, require_write
from musichub.db.session import get_db
from musichub.domain.types import Platform
from musichub.library import sources as source_service
from musichub.library import tracks as track_service
from musichub.library.tracks import TrackFilter

router = APIRouter(prefix="/api/v1/tracks", tags=["tracks"])


def _detail(db: Session, track_id: uuid.UUID) -> TrackDetailOut:
    db.expire_all()
    track = track_service.get_track(db, track_id)
    refs = [
        PlaylistRef(id=pid, name=name, occurrences=n)
        for pid, name, n in track_service.playlists_of(db, track_id)
    ]
    base = TrackOut.model_validate(track).model_dump(exclude={"platforms"})
    return TrackDetailOut(**base, playlists=refs)


@router.get("", response_model=Page[TrackOut], summary="Tracks suchen, filtern, sortieren")
def list_tracks(
    q: str | None = Query(None, max_length=200, description="Suchbegriffe (alle müssen passen)"),
    platform: list[Platform] = Query(
        [], description="Nur Tracks, die auf allen diesen Plattformen verfügbar sind"
    ),
    missing_platform: list[Platform] = Query(
        [], description="Nur Tracks, die auf diesen Plattformen nicht verfügbar sind"
    ),
    platform_count: int | None = Query(
        None, ge=0, le=len(Platform), description="Genaue Anzahl verfügbarer Plattformen"
    ),
    tag: list[uuid.UUID] = Query([], description="Nur Tracks mit allen diesen Tags"),
    has_isrc: bool | None = Query(None),
    sort: TrackSort = TrackSort.ARTIST,
    order: SortOrder = SortOrder.ASC,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[TrackOut]:
    rows, total = track_service.list_tracks(
        db,
        TrackFilter(
            q=q.strip() if q else None,
            platforms=platform,
            missing_platforms=missing_platform,
            platform_count=platform_count,
            tag_ids=tag,
            has_isrc=has_isrc,
        ),
        sort=sort,
        order=order,
        limit=limit,
        offset=offset,
    )
    return Page[TrackOut](
        items=[TrackOut.model_validate(t) for t in rows], total=total, limit=limit, offset=offset
    )


@router.get("/{track_id}", response_model=TrackDetailOut, summary="Track mit Quellen und Playlists")
def get_track(
    track_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> TrackDetailOut:
    return _detail(db, track_id)


@router.post("", response_model=TrackDetailOut, status_code=201, summary="Track anlegen")
def create_track(
    body: TrackCreate, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> TrackDetailOut:
    track = track_service.create_track(db, body)
    db.commit()
    return _detail(db, track.id)


@router.patch("/{track_id}", response_model=TrackDetailOut, summary="Track ändern")
def update_track(
    track_id: uuid.UUID,
    body: TrackUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> TrackDetailOut:
    track_service.update_track(db, track_service.get_track(db, track_id), body)
    db.commit()
    return _detail(db, track_id)


@router.delete(
    "/{track_id}",
    status_code=204,
    summary="Track löschen",
    responses={409: {"description": "Track steckt in Playlists und force ist nicht gesetzt"}},
)
def delete_track(
    track_id: uuid.UUID,
    force: bool = Query(False, description="Auch aus allen Playlists entfernen"),
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> Response:
    track_service.delete_track(db, track_id, force=force)
    db.commit()
    return Response(status_code=204)


# --- Tags eines Tracks -----------------------------------------------------------


@router.put("/{track_id}/tags", response_model=TrackDetailOut, summary="Tags eines Tracks setzen")
def set_track_tags(
    track_id: uuid.UUID,
    body: TagAssignment,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> TrackDetailOut:
    track_service.set_tags(db, track_service.get_track(db, track_id), body.tag_ids)
    db.commit()
    return _detail(db, track_id)


# --- Quellen ---------------------------------------------------------------------


@router.get("/{track_id}/sources", response_model=list[SourceOut], summary="Quellen eines Tracks")
def list_sources(
    track_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> list[SourceOut]:
    track = track_service.get_track(db, track_id)
    return [SourceOut.model_validate(s) for s in track.sources]


@router.post(
    "/{track_id}/sources",
    response_model=SourceOut,
    status_code=201,
    summary="Quelle hinzufügen (per Link oder Plattform + ID)",
)
def add_source(
    track_id: uuid.UUID,
    body: SourceCreate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> SourceOut:
    source = source_service.add_source(db, track_service.get_track(db, track_id), body)
    db.commit()
    db.refresh(source)
    return SourceOut.model_validate(source)


@router.patch("/{track_id}/sources/{source_id}", response_model=SourceOut, summary="Quelle ändern")
def update_source(
    track_id: uuid.UUID,
    source_id: uuid.UUID,
    body: SourceUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> SourceOut:
    source = source_service.update_source(
        db, source_service.get_source(db, track_id, source_id), body
    )
    db.commit()
    db.refresh(source)
    return SourceOut.model_validate(source)


@router.delete("/{track_id}/sources/{source_id}", status_code=204, summary="Quelle entfernen")
def delete_source(
    track_id: uuid.UUID,
    source_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> Response:
    source_service.delete_source(db, source_service.get_source(db, track_id, source_id))
    db.commit()
    return Response(status_code=204)
