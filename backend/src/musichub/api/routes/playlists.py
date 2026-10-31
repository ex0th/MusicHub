"""Playlists und ihre Einträge. Alle Änderungen an Einträgen liefern die komplette,
aktualisierte Playlist zurück, damit Clients nie mit veralteten Positionen arbeiten."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from musichub.api.schemas import (
    Page,
    PlaylistCreate,
    PlaylistDetailOut,
    PlaylistItemMove,
    PlaylistItemOut,
    PlaylistItemsAdd,
    PlaylistReorder,
    PlaylistSummaryOut,
    PlaylistUpdate,
    TrackOut,
)
from musichub.auth import Principal, current_principal, require_write
from musichub.db.session import get_db
from musichub.library import playlists as playlist_service

router = APIRouter(prefix="/api/v1/playlists", tags=["playlists"])


def _detail(db: Session, playlist_id: uuid.UUID) -> PlaylistDetailOut:
    db.expire_all()
    playlist = playlist_service.get_playlist(db, playlist_id)
    items = playlist_service.items_of(db, playlist_id)
    return PlaylistDetailOut(
        id=playlist.id,
        name=playlist.name,
        description=playlist.description,
        created_at=playlist.created_at,
        updated_at=playlist.updated_at,
        item_count=len(items),
        total_duration_ms=sum(i.track.duration_ms or 0 for i in items),
        items=[
            PlaylistItemOut(
                id=i.id,
                position=i.position,
                added_at=i.added_at,
                track=TrackOut.model_validate(i.track),
            )
            for i in items
        ],
    )


@router.get("", response_model=Page[PlaylistSummaryOut], summary="Playlists auflisten")
def list_playlists(
    q: str | None = Query(None, max_length=200),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[PlaylistSummaryOut]:
    rows, total = playlist_service.list_playlists(db, q=q, limit=limit, offset=offset)
    return Page[PlaylistSummaryOut](
        items=[
            PlaylistSummaryOut(
                id=p.id,
                name=p.name,
                description=p.description,
                created_at=p.created_at,
                updated_at=p.updated_at,
                item_count=n,
                total_duration_ms=ms,
            )
            for p, n, ms in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{playlist_id}", response_model=PlaylistDetailOut, summary="Playlist mit Einträgen")
def get_playlist(
    playlist_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> PlaylistDetailOut:
    return _detail(db, playlist_id)


@router.post("", response_model=PlaylistDetailOut, status_code=201, summary="Playlist anlegen")
def create_playlist(
    body: PlaylistCreate, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> PlaylistDetailOut:
    playlist = playlist_service.create_playlist(db, body)
    db.commit()
    return _detail(db, playlist.id)


@router.patch("/{playlist_id}", response_model=PlaylistDetailOut, summary="Playlist ändern")
def update_playlist(
    playlist_id: uuid.UUID,
    body: PlaylistUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> PlaylistDetailOut:
    playlist_service.update_playlist(db, playlist_service.lock_playlist(db, playlist_id), body)
    db.commit()
    return _detail(db, playlist_id)


@router.delete("/{playlist_id}", status_code=204, summary="Playlist löschen (Tracks bleiben)")
def delete_playlist(
    playlist_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> Response:
    playlist_service.delete_playlist(db, playlist_service.lock_playlist(db, playlist_id))
    db.commit()
    return Response(status_code=204)


@router.post(
    "/{playlist_id}/items",
    response_model=PlaylistDetailOut,
    summary="Tracks hinzufügen (ans Ende oder an Position)",
)
def add_items(
    playlist_id: uuid.UUID,
    body: PlaylistItemsAdd,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> PlaylistDetailOut:
    playlist_service.add_items(
        db, playlist_id, body.track_ids, position=body.position, skip_existing=body.skip_existing
    )
    db.commit()
    return _detail(db, playlist_id)


@router.delete(
    "/{playlist_id}/items/{item_id}",
    response_model=PlaylistDetailOut,
    summary="Eintrag entfernen",
)
def remove_item(
    playlist_id: uuid.UUID,
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> PlaylistDetailOut:
    playlist_service.remove_item(db, playlist_id, item_id)
    db.commit()
    return _detail(db, playlist_id)


@router.post(
    "/{playlist_id}/items/{item_id}/move",
    response_model=PlaylistDetailOut,
    summary="Eintrag an eine neue Position verschieben",
)
def move_item(
    playlist_id: uuid.UUID,
    item_id: uuid.UUID,
    body: PlaylistItemMove,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> PlaylistDetailOut:
    playlist_service.move_item(db, playlist_id, item_id, body.position)
    db.commit()
    return _detail(db, playlist_id)


@router.put(
    "/{playlist_id}/items/order",
    response_model=PlaylistDetailOut,
    summary="Komplette Reihenfolge setzen",
)
def reorder_items(
    playlist_id: uuid.UUID,
    body: PlaylistReorder,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> PlaylistDetailOut:
    playlist_service.reorder(db, playlist_id, body.item_ids)
    db.commit()
    return _detail(db, playlist_id)
