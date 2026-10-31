"""Plattform-Konten verbinden/trennen, Playlists auflisten und importieren, Jobstatus.

Alle Aktionen sind gegenüber den Plattformen **lesend**. Netzwerkzugriffe auf die
Plattformen laufen als Hintergrundjob (Worker); die API plant sie nur ein.
"""

from __future__ import annotations

import logging
import uuid
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from musichub.api.schemas import (
    AuthorizeOut,
    ConnectionAccountOut,
    ConnectionOut,
    DisconnectOut,
    JobOut,
    JobProgressOut,
    PlatformImportRequest,
    PlaylistsRefreshRequest,
    RemotePlaylistOut,
)
from musichub.auth import Principal, current_principal, require_write
from musichub.connections import service
from musichub.connectors.base import Capability
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings, get_settings
from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.db.session import get_db
from musichub.domain.types import AccountStatus, Platform
from musichub.models import Job, Playlist, RemotePlaylist

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["connections"])
#: Öffentlicher Rückruf der Plattformen – geschützt über den einmaligen ``state``
oauth_router = APIRouter(prefix="/api/oauth", tags=["connections"])

JOB_LABELS = {
    service.JOB_REFRESH_PLAYLISTS: "Playlist-Liste aktualisieren",
    service.JOB_IMPORT_PLAYLIST: "Playlist importieren",
    service.JOB_REFRESH_METADATA: "Metadaten aktualisieren",
}


def _registry(request: Request) -> ConnectorRegistry:
    registry: ConnectorRegistry = request.app.state.connectors
    return registry


def _settings(request: Request) -> Settings:
    settings: Settings = getattr(request.app.state, "settings", None) or get_settings()
    return settings


def job_out(job: Job) -> JobOut:
    payload = job.payload or {}
    platform = payload.get("platform")
    remote = payload.get("remote_playlist_id")
    return JobOut(
        id=job.id,
        kind=job.kind,
        label=JOB_LABELS.get(job.kind, job.kind),
        platform=Platform(platform) if platform in Platform._value2member_map_ else None,
        remote_playlist_id=uuid.UUID(remote) if remote else None,
        status=job.status,  # type: ignore[arg-type]
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        progress=JobProgressOut.model_validate(job.progress or {}),
        result=job.result,
        last_error=job.last_error,
        run_after=job.run_after,
        created_at=job.created_at,
        finished_at=job.finished_at,
    )


def _remote_out(rp: RemotePlaylist, names: dict[uuid.UUID, str]) -> RemotePlaylistOut:
    return RemotePlaylistOut(
        id=rp.id,
        platform=Platform(rp.platform),
        external_id=rp.external_id,
        name=rp.name,
        owner_name=rp.owner_name,
        description=rp.description,
        track_count=rp.track_count,
        url=rp.url,
        owned=rp.owned,
        items_accessible=rp.items_accessible,
        removed=rp.removed_at is not None,
        local_playlist_id=rp.local_playlist_id if rp.local_playlist_id in names else None,
        local_playlist_name=names.get(rp.local_playlist_id) if rp.local_playlist_id else None,
        last_imported_at=rp.last_imported_at,
        last_import_status=rp.last_import_status,  # type: ignore[arg-type]
        last_import_run_id=rp.last_import_run_id,
        last_error=rp.last_error,
        up_to_date=bool(rp.local_playlist_id)
        and rp.imported_version is not None
        and rp.imported_version == rp.remote_version,
    )


def _require_connected(db: Session, platform: Platform) -> None:
    account = service.require_account(db, platform)
    if account.status == AccountStatus.REAUTH_REQUIRED.value:
        raise ConflictError(
            f"Das {service.PLATFORM_LABEL[platform]}-Konto muss neu verbunden werden."
        )


# --- Übersicht ---------------------------------------------------------------------------------


@router.get("/connections", response_model=list[ConnectionOut], summary="Plattform-Konten")
def list_connections(
    request: Request,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[ConnectionOut]:
    settings = _settings(request)
    result = []
    for s in service.statuses(db, settings, _registry(request)):
        a = s.account
        result.append(
            ConnectionOut(
                platform=s.platform,
                label=service.PLATFORM_LABEL[s.platform],
                configured=s.configured,
                missing=s.missing,
                connector_state=s.connector_state,
                capabilities=s.capabilities,
                redirect_uri=service.redirect_uri(settings, s.platform)
                if settings.public_url
                else None,
                account=ConnectionAccountOut(
                    display_name=a.display_name,
                    external_user_id=a.external_user_id,
                    status=AccountStatus(a.status),
                    scopes=(a.scopes or "").split(),
                    connected_at=a.connected_at,
                    connected_by=a.connected_by,
                    last_error=a.last_error,
                    playlists_synced_at=a.playlists_synced_at,
                    last_import_at=a.last_import_at,
                )
                if a
                else None,
                playlists=s.playlists,
                imported=s.imported,
                changed=s.changed,
                failed=s.failed,
                active_jobs=s.active_jobs,
            )
        )
    return result


# --- Verbinden / Trennen -----------------------------------------------------------------------


@router.post(
    "/connections/{platform}/authorize",
    response_model=AuthorizeOut,
    summary="Anmeldung starten (liefert die Anmelde-URL der Plattform)",
)
def authorize(
    platform: Platform,
    request: Request,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> AuthorizeOut:
    url = service.start_authorization(
        db, _settings(request), _registry(request), platform, principal
    )
    db.commit()
    return AuthorizeOut(authorize_url=url)


@oauth_router.get("/callback/{platform}", include_in_schema=False)
def oauth_callback(
    platform: Platform,
    request: Request,
    state: str | None = Query(None, max_length=200),
    code: str | None = Query(None, max_length=2000),
    error: str | None = Query(None, max_length=200),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """Rückruf nach der Anmeldung. Kein Bearer-Token (Browser-Weiterleitung): Schutz über
    den einmaligen, an die Anmeldung gebundenen ``state``. Leitet zur Plattformen-Seite."""
    settings = _settings(request)
    query: dict[str, str] = {"platform": platform.value}
    try:
        service.complete_authorization(
            db, settings, _registry(request), platform, state=state, code=code, error=error
        )
        query["connected"] = "1"
    except service.AuthorizationError as exc:
        query["error"] = exc.code
        log.info("OAuth-Rückruf abgelehnt: %s", exc.code, extra={"platform": platform.value})
    db.commit()
    base = (settings.public_url or "").rstrip("/")
    response = RedirectResponse(f"{base}/platforms/?{urlencode(query)}", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.delete(
    "/connections/{platform}",
    response_model=DisconnectOut,
    summary="Konto trennen (Tokens löschen, importierte Daten bleiben)",
)
def disconnect(
    platform: Platform,
    request: Request,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> DisconnectOut:
    revoked = service.disconnect(db, _settings(request), _registry(request), platform)
    db.commit()
    return DisconnectOut(revoked=revoked)


# --- Playlists -----------------------------------------------------------------------------------


@router.get(
    "/connections/{platform}/playlists",
    response_model=list[RemotePlaylistOut],
    summary="Playlists des verbundenen Kontos (Stand der letzten Aktualisierung)",
)
def list_remote_playlists(
    platform: Platform,
    include_removed: bool = False,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[RemotePlaylistOut]:
    stmt = select(RemotePlaylist).where(RemotePlaylist.platform == platform.value)
    if not include_removed:
        stmt = stmt.where(RemotePlaylist.removed_at.is_(None))
    rows = db.scalars(stmt.order_by(RemotePlaylist.name, RemotePlaylist.id)).all()
    local_ids = [r.local_playlist_id for r in rows if r.local_playlist_id]
    names = (
        dict(db.execute(select(Playlist.id, Playlist.name).where(Playlist.id.in_(local_ids))).all())
        if local_ids
        else {}
    )
    return [_remote_out(r, names) for r in rows]


@router.post(
    "/connections/{platform}/playlists/refresh",
    response_model=JobOut,
    status_code=202,
    summary="Playlist-Liste im Hintergrund aktualisieren",
)
def refresh_playlists(
    platform: Platform,
    body: PlaylistsRefreshRequest,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> JobOut:
    _require_connected(db, platform)
    payload: dict[str, object] = {"platform": platform.value}
    if body.import_changed:
        payload["import_changed"] = True
    job = service.enqueue_unique(db, service.JOB_REFRESH_PLAYLISTS, payload)
    db.commit()
    return job_out(job)


@router.post(
    "/connections/{platform}/import",
    response_model=list[JobOut],
    status_code=202,
    summary="Ausgewählte Playlists im Hintergrund importieren",
)
def import_playlists(
    platform: Platform,
    body: PlatformImportRequest,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> list[JobOut]:
    _require_connected(db, platform)
    rows = {
        rp.id: rp
        for rp in db.scalars(
            select(RemotePlaylist).where(
                RemotePlaylist.platform == platform.value,
                RemotePlaylist.id.in_(body.playlist_ids),
            )
        )
    }
    if missing := [str(i) for i in body.playlist_ids if i not in rows]:
        raise NotFoundError("Playlist nicht gefunden.", extra={"playlist_ids": missing})
    if blocked := [rp.name for rp in rows.values() if not rp.items_accessible]:
        raise UnprocessableError(
            "Die Plattform gibt die Inhalte dieser Playlists nicht frei: " + ", ".join(blocked),
            extra={"playlists": blocked},
        )
    jobs = []
    for playlist_id in dict.fromkeys(body.playlist_ids):
        payload: dict[str, object] = {
            "platform": platform.value,
            "remote_playlist_id": str(playlist_id),
        }
        if body.force:
            payload["force"] = True
        jobs.append(
            service.enqueue_unique(db, service.JOB_IMPORT_PLAYLIST, payload, max_attempts=6)
        )
    db.commit()
    return [job_out(j) for j in jobs]


@router.post(
    "/connections/{platform}/refresh-metadata",
    response_model=JobOut,
    status_code=202,
    summary="Metadaten und Verfügbarkeit verknüpfter Tracks nachladen",
)
def refresh_metadata(
    platform: Platform,
    request: Request,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> JobOut:
    _require_connected(db, platform)
    if Capability.REFRESH_METADATA not in _registry(request).get(platform).capabilities:
        raise UnprocessableError("Für diese Plattform nicht verfügbar.")
    job = service.enqueue_unique(db, service.JOB_REFRESH_METADATA, {"platform": platform.value})
    db.commit()
    return job_out(job)


# --- Jobs ----------------------------------------------------------------------------------------


@router.get("/jobs", response_model=list[JobOut], summary="Hintergrundjobs der Plattform-Konten")
def list_jobs(
    platform: Platform | None = None,
    limit: int = Query(30, ge=1, le=200),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> list[JobOut]:
    return [job_out(j) for j in service.platform_jobs(db, platform, limit=limit)]


@router.get("/jobs/{job_id}", response_model=JobOut, summary="Ein Hintergrundjob")
def get_job(
    job_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> JobOut:
    job = db.get(Job, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} nicht gefunden.")
    return job_out(job)
