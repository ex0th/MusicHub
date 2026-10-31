"""Track-Quellen: Verweise eines lokalen Tracks auf Spotify, SoundCloud oder YouTube."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from musichub.api.schemas import SourceCreate, SourceUpdate
from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.domain.platform_urls import (
    PlatformUrlError,
    canonical_url,
    parse_url,
    validate_external_id,
)
from musichub.domain.types import Availability, LinkStatus, MatchMethod, Platform
from musichub.models import Track, TrackSource


def _resolve(
    platform: Platform | None, external_id: str | None, url: str | None
) -> tuple[Platform, str, str]:
    """Plattform, ID und URL aus den Eingaben bestimmen und prüfen."""
    try:
        if url:
            parsed = parse_url(url)
            if platform and platform is not parsed.platform:
                raise UnprocessableError(
                    f"Link gehört zu {parsed.platform.value}, nicht zu {platform.value}."
                )
            if (
                external_id
                and validate_external_id(parsed.platform, external_id) != parsed.external_id
            ):
                raise UnprocessableError("external_id passt nicht zum Link.")
            return parsed.platform, parsed.external_id, parsed.url
        assert platform and external_id  # durch SourceCreate garantiert
        clean_id = validate_external_id(platform, external_id)
        return platform, clean_id, canonical_url(platform, clean_id)
    except PlatformUrlError as exc:
        raise UnprocessableError(str(exc)) from exc


def _check_conflicts(
    db: Session,
    *,
    track_id: uuid.UUID,
    platform: Platform,
    external_id: str,
    link_status: LinkStatus,
    exclude_id: uuid.UUID | None = None,
) -> None:
    base = select(TrackSource).where(TrackSource.platform == platform.value)
    if exclude_id:
        base = base.where(TrackSource.id != exclude_id)

    same = db.scalars(
        base.where(TrackSource.track_id == track_id, TrackSource.external_id == external_id)
    ).first()
    if same:
        raise ConflictError(
            "Diese Quelle ist dem Track bereits zugeordnet.", extra={"source_id": str(same.id)}
        )
    if link_status is not LinkStatus.LINKED:
        return
    linked_here = db.scalars(
        base.where(
            TrackSource.track_id == track_id,
            TrackSource.link_status == LinkStatus.LINKED.value,
        )
    ).first()
    if linked_here:
        raise ConflictError(
            f"Der Track hat bereits eine bestätigte {platform.value}-Quelle. "
            "Diese zuerst ändern oder entfernen.",
            extra={"source_id": str(linked_here.id)},
        )
    linked_elsewhere = db.scalars(
        base.where(
            TrackSource.external_id == external_id,
            TrackSource.link_status == LinkStatus.LINKED.value,
        )
    ).first()
    if linked_elsewhere:
        raise ConflictError(
            f"Dieser {platform.value}-Track ist bereits mit einem anderen Track verknüpft.",
            extra={"track_id": str(linked_elsewhere.track_id)},
        )


def _flush(db: Session) -> None:
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError as exc:  # gleichzeitige Änderung zwischen Prüfung und Schreiben
        raise ConflictError("Die Quelle kollidiert mit einer bestehenden Zuordnung.") from exc


def add_source(db: Session, track: Track, data: SourceCreate) -> TrackSource:
    platform, external_id, url = _resolve(data.platform, data.external_id, data.url)
    _check_conflicts(
        db,
        track_id=track.id,
        platform=platform,
        external_id=external_id,
        link_status=data.link_status,
    )
    source = TrackSource(
        track_id=track.id,
        platform=platform.value,
        external_id=external_id,
        # Vom Nutzer eingegebene Links behalten (z. B. mit Zeitmarke), sonst kanonisch
        url=data.url or url,
        link_status=data.link_status.value,
        availability=data.availability.value,
        match_method=MatchMethod.MANUAL.value,
        last_checked_at=None if data.availability is Availability.UNKNOWN else datetime.now(UTC),
    )
    db.add(source)
    track.updated_at = func.now()
    _flush(db)
    db.refresh(track, ["sources"])
    return source


def get_source(db: Session, track_id: uuid.UUID, source_id: uuid.UUID) -> TrackSource:
    source = db.get(TrackSource, source_id)
    if source is None or source.track_id != track_id:
        raise NotFoundError(f"Quelle {source_id} nicht gefunden.")
    return source


def update_source(db: Session, source: TrackSource, data: SourceUpdate) -> TrackSource:
    changes = data.model_dump(exclude_unset=True)
    platform = Platform(source.platform)

    new_external_id = source.external_id
    if "external_id" in changes or (changes.get("url")):
        _, new_external_id, canonical = _resolve(
            platform, changes.get("external_id"), changes.get("url")
        )
        if "url" not in changes:
            changes["url"] = canonical
    new_status = LinkStatus(changes.get("link_status") or source.link_status)

    if new_external_id != source.external_id or (
        new_status is LinkStatus.LINKED and source.link_status != LinkStatus.LINKED.value
    ):
        _check_conflicts(
            db,
            track_id=source.track_id,
            platform=platform,
            external_id=new_external_id,
            link_status=new_status,
            exclude_id=source.id,
        )

    source.external_id = new_external_id
    if "url" in changes:
        source.url = changes["url"] or canonical_url(platform, new_external_id)
    if changes.get("link_status"):
        source.link_status = new_status.value
    if changes.get("availability"):
        source.availability = Availability(changes["availability"]).value
        source.last_checked_at = changes.get("last_checked_at") or datetime.now(UTC)
    elif "last_checked_at" in changes:
        source.last_checked_at = changes["last_checked_at"]

    source.track.updated_at = func.now()
    _flush(db)
    return source


def delete_source(db: Session, source: TrackSource) -> None:
    source.track.updated_at = func.now()
    db.delete(source)
    db.flush()
