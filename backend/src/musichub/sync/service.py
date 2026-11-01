"""Synchronisationsziele, Vorschau/Sync anfordern, Historie, nicht zugeordnete Tracks, Zeitplan.

Sicherheitsregeln (siehe docs/SYNC.md):

1. Geschrieben wird nur, wenn die Plattform in ``MUSICHUB_SYNC_WRITE_PLATFORMS`` steht **und**
   das Konto mit Schreibrechten verbunden wurde **und** das Ziel aktiviert ist.
2. Vor dem ersten schreibenden Sync eines Ziels muss eine Vorschau bestätigt werden
   (``preview_run_id``, höchstens ``PREVIEW_MAX_AGE`` alt).
3. „Vollständig abgleichen“ nur für von MusicHub angelegte (``managed``) und ausdrücklich
   freigegebene (``mirror_allowed``) Ziele – auch per CHECK-Constraint abgesichert.
4. Externe Tracks entfernen bzw. extern Entferntes wieder hinzufügen nur mit ausdrücklicher
   Zustimmung zu einer gesehenen Vorschau. Geplante Läufe stimmen nie zu.
5. Je Ziel läuft höchstens ein Lauf gleichzeitig.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from musichub.api.schemas import SyncTargetCreate, SyncTargetUpdate
from musichub.auth import Principal
from musichub.connections import service as connections
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings
from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.domain.types import (
    AccountStatus,
    LinkStatus,
    LookupStatus,
    Platform,
    SyncMode,
    SyncRunStatus,
    SyncTrigger,
)
from musichub.jobs.queue import enqueue
from musichub.models import (
    PlatformLookup,
    Playlist,
    PlaylistItem,
    PlaylistTarget,
    RemotePlaylist,
    SyncRun,
    Track,
)

log = logging.getLogger(__name__)

JOB_SYNC = "sync.run"
PREVIEW_MAX_AGE = timedelta(hours=1)
ACTIVE_RUN = (SyncRunStatus.QUEUED.value, SyncRunStatus.RUNNING.value)


def _now() -> datetime:
    return datetime.now(UTC)


# --- Schreibberechtigung ----------------------------------------------------------------------


def write_blockers(
    db: Session, settings: Settings, registry: ConnectorRegistry, target: PlaylistTarget
) -> list[str]:
    """Warum dieses Ziel (noch) nicht beschrieben werden darf. Leer = bereit."""
    platform = Platform(target.platform)
    label = connections.PLATFORM_LABEL[platform]
    connector = registry.get(platform)
    problems: list[str] = []
    if not connector.write_scopes:
        problems.append(f"Synchronisierung nach {label} wird nicht unterstützt.")
        return problems
    if not settings.sync_write_enabled(platform.value):
        problems.append(
            f"Schreiben nach {label} ist nicht freigeschaltet (MUSICHUB_SYNC_WRITE_PLATFORMS)."
        )
    account = connections.get_account(db, platform)
    if account is None:
        problems.append(f"Kein {label}-Konto verbunden.")
    elif account.status == AccountStatus.REAUTH_REQUIRED.value:
        problems.append(f"Das {label}-Konto muss neu verbunden werden.")
    elif not connector.has_write_scope(account.scopes):
        problems.append(
            f"Das {label}-Konto ist nur lesend verbunden – mit Schreibzugriff neu verbinden."
        )
    if not target.enabled:
        problems.append("Synchronisierung für dieses Ziel ist deaktiviert.")
    return problems


# --- Ziele ---------------------------------------------------------------------------------------


def get_target(
    db: Session, target_id: uuid.UUID, *, playlist_id: uuid.UUID | None = None
) -> PlaylistTarget:
    target = db.get(PlaylistTarget, target_id)
    if target is None or (playlist_id and target.playlist_id != playlist_id):
        raise NotFoundError(f"Synchronisationsziel {target_id} nicht gefunden.")
    return target


def list_targets(db: Session, playlist_id: uuid.UUID) -> list[PlaylistTarget]:
    return list(
        db.scalars(
            select(PlaylistTarget)
            .where(PlaylistTarget.playlist_id == playlist_id)
            .order_by(PlaylistTarget.created_at, PlaylistTarget.id)
        )
    )


def active_run(db: Session, target_id: uuid.UUID) -> SyncRun | None:
    return db.scalar(
        select(SyncRun)
        .where(SyncRun.target_id == target_id, SyncRun.status.in_(ACTIVE_RUN))
        .order_by(SyncRun.created_at.desc())
        .limit(1)
    )


def _check_mode(mode: SyncMode, managed: bool, mirror_allowed: bool) -> None:
    if mode is SyncMode.MIRROR and not (managed and mirror_allowed):
        raise UnprocessableError(
            "„Vollständig abgleichen“ ist nur für von MusicHub angelegte und ausdrücklich "
            "freigegebene Zielplaylists möglich."
        )


def create_target(
    db: Session,
    registry: ConnectorRegistry,
    playlist_id: uuid.UUID,
    data: SyncTargetCreate,
    principal: Principal,
) -> PlaylistTarget:
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        raise NotFoundError(f"Playlist {playlist_id} nicht gefunden.")
    platform = data.platform
    label = connections.PLATFORM_LABEL[platform]
    if not registry.get(platform).write_scopes:
        raise UnprocessableError(f"Synchronisierung nach {label} wird nicht unterstützt.")
    connections.require_account(db, platform)

    target = PlaylistTarget(
        playlist_id=playlist_id,
        platform=platform.value,
        enabled=data.enabled,
        created_by=principal.subject,
        schedule_minutes=data.schedule_minutes,
        next_run_at=_now() + timedelta(minutes=data.schedule_minutes)
        if data.schedule_minutes
        else None,
    )
    if data.create_new:
        target.managed = True
        target.mirror_allowed = data.mirror_allowed
        target.remote_name = (data.remote_name or playlist.name)[:150]
    else:
        remote = db.get(RemotePlaylist, data.remote_playlist_id)
        if remote is None or remote.platform != platform.value or remote.removed_at:
            raise NotFoundError("Plattform-Playlist nicht gefunden.")
        if not (remote.owned or remote.items_accessible):
            raise UnprocessableError(
                f"In „{remote.name}“ kann nicht geschrieben werden (nicht dein Eigentum)."
            )
        if not remote.items_accessible:
            raise UnprocessableError(f"{label} gibt die Inhalte von „{remote.name}“ nicht frei.")
        if data.mirror_allowed:
            raise UnprocessableError(
                "Vorhandene Playlists werden nicht von MusicHub verwaltet – nur „Nur neue "
                "Tracks hinzufügen“ ist möglich."
            )
        target.remote_playlist_id = remote.external_id
        target.remote_name = remote.name
        target.remote_url = remote.url
    _check_mode(data.mode, target.managed, target.mirror_allowed)
    target.mode = data.mode.value
    db.add(target)
    try:
        with db.begin_nested():
            db.flush()
    except IntegrityError:
        raise ConflictError(
            "Diese Plattform-Playlist ist bereits Ziel einer anderen Synchronisierung."
        ) from None
    return target


def update_target(db: Session, target: PlaylistTarget, data: SyncTargetUpdate) -> PlaylistTarget:
    if active_run(db, target.id):
        raise ConflictError("Während eines laufenden Syncs kann das Ziel nicht geändert werden.")
    if data.enabled is not None:
        target.enabled = data.enabled
    if data.mirror_allowed is not None:
        if data.mirror_allowed and not target.managed:
            raise UnprocessableError(
                "Nur von MusicHub angelegte Playlists können vollständig abgeglichen werden."
            )
        target.mirror_allowed = data.mirror_allowed
        if not data.mirror_allowed and target.mode == SyncMode.MIRROR.value:
            target.mode = SyncMode.ADD_ONLY.value
    if data.mode is not None and data.mode.value != target.mode:
        _check_mode(data.mode, target.managed, target.mirror_allowed)
        target.mode = data.mode.value
        if data.mode is SyncMode.MIRROR:
            # Strengerer Modus: neue Vorschau muss bestätigt werden
            target.confirmed_at = None
            target.confirmed_by = None
    if data.manual_only:
        target.schedule_minutes = None
        target.next_run_at = None
    elif data.schedule_minutes is not None:
        target.schedule_minutes = data.schedule_minutes
        target.next_run_at = _now() + timedelta(minutes=data.schedule_minutes)
    db.flush()
    return target


def delete_target(db: Session, target: PlaylistTarget) -> None:
    """Entfernt nur die Einstellung – die Plattform-Playlist bleibt unangetastet."""
    if active_run(db, target.id):
        raise ConflictError("Während eines laufenden Syncs kann das Ziel nicht entfernt werden.")
    db.delete(target)
    db.flush()


# --- Läufe anfordern -----------------------------------------------------------------------------


def _new_run(
    db: Session,
    target: PlaylistTarget,
    *,
    trigger: SyncTrigger,
    dry_run: bool,
    created_by: str,
    preview_run_id: uuid.UUID | None = None,
    approved: bool = False,
) -> SyncRun:
    playlist = db.get(Playlist, target.playlist_id)
    assert playlist is not None
    run = SyncRun(
        target_id=target.id,
        playlist_id=target.playlist_id,
        playlist_name=playlist.name,
        platform=target.platform,
        remote_playlist_id=target.remote_playlist_id,
        remote_name=target.remote_name,
        trigger=trigger.value,
        status=SyncRunStatus.QUEUED.value,
        dry_run=dry_run,
        mode=target.mode,
        approved_external_removal=approved,
        preview_run_id=preview_run_id,
        created_by=created_by,
        plan={},
        errors=[],
        added=0,
        removed=0,
        skipped=0,
        unmatched=0,
    )
    db.add(run)
    db.flush()
    # Höchstens ein Versuch mehr nach Rate-Limit/Kontingent – keine Endlosschleifen
    job = enqueue(
        db, JOB_SYNC, {"run_id": str(run.id), "platform": target.platform}, max_attempts=3
    )
    run.job_id = job.id
    db.flush()
    return run


def request_preview(db: Session, target: PlaylistTarget, principal: Principal) -> SyncRun:
    connections.require_account(db, Platform(target.platform))
    if (running := active_run(db, target.id)) is not None:
        if running.dry_run:
            return running
        raise ConflictError("Für dieses Ziel läuft bereits eine Synchronisierung.")
    return _new_run(
        db, target, trigger=SyncTrigger.PREVIEW, dry_run=True, created_by=principal.subject
    )


def request_sync(
    db: Session,
    settings: Settings,
    registry: ConnectorRegistry,
    target: PlaylistTarget,
    principal: Principal,
    *,
    preview_run_id: uuid.UUID | None,
    approve_external_changes: bool,
) -> SyncRun:
    if problems := write_blockers(db, settings, registry, target):
        raise ConflictError(" ".join(problems), extra={"blockers": problems})
    if active_run(db, target.id) is not None:
        raise ConflictError("Für dieses Ziel läuft bereits eine Synchronisierung.")
    preview: SyncRun | None = None
    if preview_run_id is not None:
        preview = db.get(SyncRun, preview_run_id)
        if (
            preview is None
            or preview.target_id != target.id
            or preview.status != SyncRunStatus.PREVIEW.value
        ):
            raise UnprocessableError("Die angegebene Vorschau gehört nicht zu diesem Ziel.")
        if preview.mode != target.mode:
            raise UnprocessableError("Die Vorschau wurde mit einem anderen Modus erstellt.")
        if _now() - (preview.finished_at or preview.created_at) > PREVIEW_MAX_AGE:
            raise UnprocessableError("Die Vorschau ist veraltet. Bitte eine neue erstellen.")
    if target.confirmed_at is None and preview is None:
        raise ConflictError(
            "Vor der ersten Synchronisierung muss eine Vorschau bestätigt werden.",
            extra={"needs_confirmation": True},
        )
    if approve_external_changes and preview is None:
        raise UnprocessableError(
            "Die Zustimmung zu externen Änderungen gilt nur für eine gesehene Vorschau."
        )
    if target.confirmed_at is None:
        target.confirmed_at = _now()
        target.confirmed_by = principal.subject
    return _new_run(
        db,
        target,
        trigger=SyncTrigger.MANUAL,
        dry_run=False,
        created_by=principal.subject,
        preview_run_id=preview.id if preview else None,
        approved=approve_external_changes,
    )


# --- Zeitplan ------------------------------------------------------------------------------------


def enqueue_due(db: Session, settings: Settings, registry: ConnectorRegistry) -> int:
    """Fällige geplante Synchronisierungen einplanen (vom Worker regelmäßig aufgerufen).

    Geplante Läufe setzen eine bestätigte erste Synchronisierung voraus und stimmen
    externen Änderungen nie zu."""
    now = _now()
    due = db.scalars(
        select(PlaylistTarget)
        .where(
            PlaylistTarget.enabled.is_(True),
            PlaylistTarget.schedule_minutes.is_not(None),
            PlaylistTarget.confirmed_at.is_not(None),
            PlaylistTarget.next_run_at <= now,
        )
        .with_for_update(skip_locked=True)
    ).all()
    count = 0
    for target in due:
        assert target.schedule_minutes is not None
        target.next_run_at = now + timedelta(minutes=target.schedule_minutes)
        if active_run(db, target.id) is not None:
            continue
        if write_blockers(db, settings, registry, target):
            continue
        _new_run(db, target, trigger=SyncTrigger.SCHEDULED, dry_run=False, created_by="zeitplan")
        count += 1
    db.flush()
    return count


# --- Historie und nicht zugeordnete Tracks -------------------------------------------------------


def history(
    db: Session,
    *,
    playlist_id: uuid.UUID,
    target_id: uuid.UUID | None,
    limit: int,
    offset: int,
) -> tuple[list[SyncRun], int]:
    from sqlalchemy import func

    stmt = select(SyncRun).where(SyncRun.playlist_id == playlist_id)
    if target_id:
        stmt = stmt.where(SyncRun.target_id == target_id)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(SyncRun.created_at.desc(), SyncRun.id).limit(limit).offset(offset)
    ).all()
    return list(rows), total


def get_run(db: Session, run_id: uuid.UUID, *, playlist_id: uuid.UUID) -> SyncRun:
    run = db.get(SyncRun, run_id)
    if run is None or run.playlist_id != playlist_id:
        raise NotFoundError(f"Synchronisationslauf {run_id} nicht gefunden.")
    return run


def unresolved_tracks(
    db: Session, playlist_id: uuid.UUID, platform: Platform
) -> list[tuple[Track, str, PlatformLookup | None]]:
    """Tracks der Playlist ohne nutzbare Verknüpfung zur Plattform, mit Grund."""
    from musichub.sync.executor import resolve_reason

    track_ids = list(
        dict.fromkeys(
            db.scalars(
                select(PlaylistItem.track_id)
                .where(PlaylistItem.playlist_id == playlist_id)
                .order_by(PlaylistItem.position)
            )
        )
    )
    tracks = {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(track_ids)))}
    lookups = {
        lk.track_id: lk
        for lk in db.scalars(
            select(PlatformLookup).where(
                PlatformLookup.platform == platform.value, PlatformLookup.track_id.in_(track_ids)
            )
        )
    }
    result = []
    for track_id in track_ids:
        track = tracks[track_id]
        reason = resolve_reason(track, platform, lookups.get(track_id))
        if reason is not None:
            result.append((track, reason, lookups.get(track_id)))
    return result


def recheck(db: Session, platform: Platform, track_ids: list[uuid.UUID]) -> int:
    """Suchergebnisse verwerfen – beim nächsten Lauf wird erneut gesucht."""
    result = db.execute(
        delete(PlatformLookup).where(
            PlatformLookup.platform == platform.value,
            PlatformLookup.track_id.in_(track_ids),
            PlatformLookup.status != LookupStatus.FOUND.value,
        )
    )
    db.flush()
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def linked_platforms(track: Track) -> list[str]:
    return [s.platform for s in track.sources if s.link_status == LinkStatus.LINKED.value]
