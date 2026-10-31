from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from musichub.api.schemas import TagCreate, TagOut, TagUpdate, TagWithCountOut
from musichub.auth import Principal, current_principal, require_write
from musichub.db.session import get_db
from musichub.library import tags as tag_service

router = APIRouter(prefix="/api/v1/tags", tags=["tags"])


@router.get("", response_model=list[TagWithCountOut], summary="Alle Tags mit Anzahl Tracks")
def list_tags(
    q: str | None = Query(None, max_length=100),
    category: str | None = Query(None, max_length=50),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[TagWithCountOut]:
    return [
        TagWithCountOut(id=t.id, name=t.name, category=t.category, track_count=n)
        for t, n in tag_service.list_tags(db, q=q, category=category)
    ]


@router.post("", response_model=TagOut, status_code=201, summary="Tag anlegen")
def create_tag(
    body: TagCreate, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> TagOut:
    tag = tag_service.create_tag(db, body)
    db.commit()
    return TagOut.model_validate(tag)


@router.patch("/{tag_id}", response_model=TagOut, summary="Tag umbenennen/umsortieren")
def update_tag(
    tag_id: uuid.UUID,
    body: TagUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> TagOut:
    tag = tag_service.update_tag(db, tag_service.get_tag(db, tag_id), body)
    db.commit()
    return TagOut.model_validate(tag)


@router.delete("/{tag_id}", status_code=204, summary="Tag löschen (Tracks bleiben erhalten)")
def delete_tag(
    tag_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> Response:
    tag_service.delete_tag(db, tag_service.get_tag(db, tag_id))
    db.commit()
    return Response(status_code=204)
