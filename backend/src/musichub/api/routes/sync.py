"""Synchronisierung lokaler Playlists auf Plattformen (MusicHub → Plattform)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response
from sqlalchemy.orm import Session

from musichub.api.schemas import (
    Page,
    RecheckRequest,
    SourceOut,
    SyncRequest,
    SyncRunOut,
    SyncTargetCreate,
    SyncTargetOut,
    SyncTargetUpdate,
    UnresolvedTrackOut,
)
from musichub.auth import Principal, current_principal, require_write
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings, get_settings
from musichub.core.errors import NotFoundError
from musichub.db.session import get_db
from musichub.domain.platform_urls import canonical_url
from musichub.domain.types import LinkStatus, Platform
from musichub.models import Playlist, PlaylistTarget, SyncRun
from musichub.sync import service

router = APIRouter(prefix="/api/v1/playlists/{playlist_id}/sync", tags=["sync"])


def _registry(request: Request) -> ConnectorRegistry:
    registry: ConnectorRegistry = request.app.state.connectors
    return registry


def _settings(request: Request) -> Settings:
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return settings


def _playlist(db: Session, playlist_id: uuid.UUID) -> Playlist:
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        raise NotFoundError(f"Playlist {playlist_id} nicht gefunden.")
    return playlist


def target_out(db: Session, request: Request, target: PlaylistTarget) -> SyncTargetOut:
    blockers = service.write_blockers(db, _settings(request), _registry(request), target)
    running = service.active_run(db, target.id)
    return SyncTargetOut(
        id=target.id,
        playlist_id=target.playlist_id,
        platform=Platform(target.platform),
        remote_playlist_id=target.remote_playlist_id,
        remote_name=target.remote_name,
        remote_url=target.remote_url,
        enabled=target.enabled,
        mode=target.mode,  # type: ignore[arg-type]
        managed=target.managed,
        mirror_allowed=target.mirror_allowed,
        schedule_minutes=target.schedule_minutes,
        next_run_at=target.next_run_at,
        confirmed_at=target.confirmed_at,
        last_synced_at=target.last_synced_at,
        last_status=target.last_status,  # type: ignore[arg-type]
        last_run_id=target.last_run_id,
        last_error=target.last_error,
        write_ready=not blockers,
        write_blockers=blockers,
        needs_confirmation=target.confirmed_at is None,
        active_run_id=running.id if running else None,
    )


def run_out(run: SyncRun, *, with_plan: bool = False) -> SyncRunOut:
    return SyncRunOut.model_validate(
        {
            **{k: getattr(run, k) for k in SyncRunOut.model_fields if k != "plan"},
            "plan": run.plan if with_plan else None,
        }
    )


# --- Ziele -----------------------------------------------------------------------------------


@router.get("/targets", response_model=list[SyncTargetOut], summary="Synchronisationsziele")
def list_targets(
    playlist_id: uuid.UUID,
    request: Request,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[SyncTargetOut]:
    _playlist(db, playlist_id)
    return [target_out(db, request, t) for t in service.list_targets(db, playlist_id)]


@router.post("/targets", response_model=SyncTargetOut, status_code=201, summary="Ziel hinzufügen")
def create_target(
    playlist_id: uuid.UUID,
    body: SyncTargetCreate,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> SyncTargetOut:
    target = service.create_target(db, _registry(request), playlist_id, body, principal)
    db.commit()
    return target_out(db, request, target)


@router.patch("/targets/{target_id}", response_model=SyncTargetOut, summary="Ziel ändern")
def update_target(
    playlist_id: uuid.UUID,
    target_id: uuid.UUID,
    body: SyncTargetUpdate,
    request: Request,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> SyncTargetOut:
    target = service.update_target(
        db, service.get_target(db, target_id, playlist_id=playlist_id), body
    )
    db.commit()
    return target_out(db, request, target)


@router.delete(
    "/targets/{target_id}",
    status_code=204,
    summary="Ziel entfernen (die Plattform-Playlist bleibt unverändert)",
)
def delete_target(
    playlist_id: uuid.UUID,
    target_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> Response:
    service.delete_target(db, service.get_target(db, target_id, playlist_id=playlist_id))
    db.commit()
    return Response(status_code=204)


# --- Vorschau und Sync -------------------------------------------------------------------------


@router.post(
    "/targets/{target_id}/preview",
    response_model=SyncRunOut,
    status_code=202,
    summary="Vorschau (Dry-Run) – ändert nichts auf der Plattform",
)
def preview(
    playlist_id: uuid.UUID,
    target_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> SyncRunOut:
    run = service.request_preview(
        db, service.get_target(db, target_id, playlist_id=playlist_id), principal
    )
    db.commit()
    return run_out(run)


@router.post(
    "/targets/{target_id}/sync",
    response_model=SyncRunOut,
    status_code=202,
    summary="Synchronisieren (beim ersten Mal nur mit bestätigter Vorschau)",
)
def sync(
    playlist_id: uuid.UUID,
    target_id: uuid.UUID,
    body: SyncRequest,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> SyncRunOut:
    run = service.request_sync(
        db,
        _settings(request),
        _registry(request),
        service.get_target(db, target_id, playlist_id=playlist_id),
        principal,
        preview_run_id=body.preview_run_id,
        approve_external_changes=body.approve_external_changes,
    )
    db.commit()
    return run_out(run)


# --- Historie ----------------------------------------------------------------------------------


@router.get("/runs", response_model=Page[SyncRunOut], summary="Synchronisationshistorie")
def runs(
    playlist_id: uuid.UUID,
    target_id: uuid.UUID | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[SyncRunOut]:
    rows, total = service.history(
        db, playlist_id=playlist_id, target_id=target_id, limit=limit, offset=offset
    )
    return Page[SyncRunOut](
        items=[run_out(r) for r in rows], total=total, limit=limit, offset=offset
    )


@router.get("/runs/{run_id}", response_model=SyncRunOut, summary="Lauf mit Plan bzw. Vorschau")
def get_run(
    playlist_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> SyncRunOut:
    return run_out(service.get_run(db, run_id, playlist_id=playlist_id), with_plan=True)


# --- Nicht zugeordnete Tracks --------------------------------------------------------------------


@router.get(
    "/unresolved",
    response_model=list[UnresolvedTrackOut],
    summary="Tracks ohne nutzbare Verknüpfung zur Plattform",
)
def unresolved(
    playlist_id: uuid.UUID,
    platform: Platform,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[UnresolvedTrackOut]:
    _playlist(db, playlist_id)
    result = []
    for track, reason, lookup in service.unresolved_tracks(db, playlist_id, platform):
        result.append(
            UnresolvedTrackOut(
                track_id=track.id,
                title=track.title,
                artist=track.artist,
                version=track.version,
                reason=reason,
                candidate_url=canonical_url(platform, lookup.external_id)
                if lookup and lookup.external_id
                else None,
                checked_at=lookup.checked_at if lookup else None,
                message=lookup.message if lookup else None,
                alternatives=[
                    SourceOut.model_validate(s)
                    for s in track.sources
                    if s.platform != platform.value and s.link_status == LinkStatus.LINKED.value
                ],
            )
        )
    return result


@router.post(
    "/recheck",
    summary="Suchergebnisse verwerfen – beim nächsten Lauf erneut prüfen",
)
def recheck(
    playlist_id: uuid.UUID,
    platform: Platform,
    body: RecheckRequest,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> dict[str, int]:
    _playlist(db, playlist_id)
    count = service.recheck(db, platform, body.track_ids)
    db.commit()
    return {"reset": count}
