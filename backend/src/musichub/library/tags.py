"""Tags verwalten. Namen sind pro Kategorie eindeutig (ohne Groß/Klein)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from musichub.api.schemas import TagCreate, TagUpdate
from musichub.core.errors import ConflictError, NotFoundError
from musichub.models import Tag, track_tags


def list_tags(
    db: Session, *, q: str | None = None, category: str | None = None
) -> Sequence[tuple[Tag, int]]:
    count = (
        select(func.count())
        .select_from(track_tags)
        .where(track_tags.c.tag_id == Tag.id)
        .scalar_subquery()
    )
    stmt = select(Tag, count)
    if q:
        stmt = stmt.where(Tag.name.ilike(f"%{q}%"))
    if category:
        stmt = stmt.where(func.lower(Tag.category) == category.lower())
    stmt = stmt.order_by(func.lower(Tag.category), func.lower(Tag.name))
    return [(row[0], row[1]) for row in db.execute(stmt).all()]


def get_tag(db: Session, tag_id: uuid.UUID) -> Tag:
    tag = db.get(Tag, tag_id)
    if tag is None:
        raise NotFoundError(f"Tag {tag_id} nicht gefunden.")
    return tag


def _ensure_unique(db: Session, name: str, category: str, exclude: uuid.UUID | None) -> None:
    stmt = select(Tag.id).where(
        func.lower(Tag.name) == name.lower(), func.lower(Tag.category) == category.lower()
    )
    if exclude:
        stmt = stmt.where(Tag.id != exclude)
    existing = db.scalar(stmt)
    if existing:
        raise ConflictError(
            f"Tag „{name}“ existiert in Kategorie „{category}“ bereits.",
            extra={"tag_id": str(existing)},
        )


def _flush(db: Session) -> None:
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:
        raise ConflictError("Tag existiert bereits.") from exc


def create_tag(db: Session, data: TagCreate) -> Tag:
    _ensure_unique(db, data.name, data.category, None)
    tag = Tag(name=data.name, category=data.category)
    db.add(tag)
    _flush(db)
    return tag


def update_tag(db: Session, tag: Tag, data: TagUpdate) -> Tag:
    name = data.name or tag.name
    category = data.category or tag.category
    _ensure_unique(db, name, category, tag.id)
    tag.name, tag.category = name, category
    _flush(db)
    return tag


def delete_tag(db: Session, tag: Tag) -> None:
    db.delete(tag)
    db.flush()
