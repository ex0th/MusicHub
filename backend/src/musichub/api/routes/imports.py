"""Import-Assistent: hochladen/analysieren, Vorschau, Entscheidungen, bestätigen, Bericht."""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from musichub.api.schemas import (
    ImportDecisionUpdate,
    ImportFormatOut,
    ImportFormatsOut,
    ImportItemOut,
    ImportOptionsUpdate,
    ImportPlaylistPlan,
    ImportRunOut,
    ImportSourceOut,
    ImportTrackRef,
    ImportUnassigned,
    Page,
)
from musichub.auth import Principal, current_principal, require_write
from musichub.core.errors import AppError, PayloadTooLargeError
from musichub.db.session import get_db
from musichub.domain.types import ImportItemStatus
from musichub.library import matching as matching_service
from musichub.models import ImportItem, ImportRun, Track
from musichub.transfer import service
from musichub.transfer.decoding import ALLOWED_EXTENSIONS, human_size
from musichub.transfer.export import to_csv
from musichub.transfer.formats import FORMATS

router = APIRouter(prefix="/api/v1/imports", tags=["import"])


def _settings(request: Request) -> Any:
    return request.app.state.settings


def _limit_upload(request: Request) -> None:
    """Zu große Uploads früh ablehnen (die genaue Prüfung folgt nach dem Lesen)."""
    max_bytes = _settings(request).import_max_bytes
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > max_bytes + 64 * 1024:
        raise PayloadTooLargeError(f"Die Datei ist größer als {human_size(max_bytes)}.")


def run_out(run: ImportRun) -> ImportRunOut:
    options = run.options or {}
    unassigned = options.get("unassigned")
    return ImportRunOut(
        id=run.id,
        filename=run.filename,
        size_bytes=run.size_bytes,
        format=run.format,
        format_label=service.format_label(run.format),
        status=run.status,
        created_by=run.created_by,
        created_at=run.created_at,
        committed_at=run.committed_at,
        warnings=run.warnings or [],
        error=run.error,
        summary=run.summary or {},
        playlists=[
            ImportPlaylistPlan(
                name=p["name"],
                description=p.get("description"),
                track_count=len(p["entries"]),
                existing_playlist_id=p.get("existing_playlist_id"),
                mode=p["mode"],
            )
            for p in options.get("playlists", [])
        ],
        unassigned=ImportUnassigned(**unassigned) if unassigned else None,
    )


def _items_out(db: Session, items: list[ImportItem]) -> list[ImportItemOut]:
    match_ids = {i.match_track_id for i in items if i.match_track_id}
    tracks = (
        {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(match_ids)))}
        if match_ids
        else {}
    )
    rows_by_seq = (
        {
            seq: row
            for seq, row in db.execute(
                select(ImportItem.seq, ImportItem.row).where(
                    ImportItem.run_id == items[0].run_id,
                    ImportItem.seq.in_(
                        {i.duplicate_of_seq for i in items if i.duplicate_of_seq is not None}
                    ),
                )
            )
        }
        if items
        else {}
    )
    out = []
    for i in items:
        d = i.data
        match = tracks.get(i.match_track_id) if i.match_track_id else None
        out.append(
            ImportItemOut(
                id=i.id,
                row=i.row,
                status=i.status,
                decision=i.decision,
                title=d.get("title"),
                artist=d.get("artist"),
                version=d.get("version"),
                album=d.get("album"),
                duration_ms=d.get("duration_ms"),
                isrc=d.get("isrc"),
                sources=[ImportSourceOut(**s) for s in d.get("sources", [])],
                playlists=i.playlists or [],
                errors=d.get("errors", []),
                warnings=d.get("warnings", []),
                match=ImportTrackRef(
                    id=match.id, title=match.title, artist=match.artist, version=match.version
                )
                if match
                else None,
                match_reason=i.match_reason,
                match_score=i.match_score,
                duplicate_of_row=rows_by_seq.get(i.duplicate_of_seq)
                if i.duplicate_of_seq is not None
                else None,
                result=i.result,
                result_track_id=i.result_track_id,
                result_message=i.result_message,
            )
        )
    return out


@router.get("/formats", response_model=ImportFormatsOut, summary="Unterstützte Formate und Grenzen")
def formats(request: Request, _: Principal = Depends(current_principal)) -> ImportFormatsOut:
    settings = _settings(request)
    return ImportFormatsOut(
        formats=[ImportFormatOut(id=f.id, label=f.label, kind=f.kind) for f in FORMATS.values()],
        max_bytes=settings.import_max_bytes,
        max_rows=settings.import_max_rows,
        extensions=sorted(ALLOWED_EXTENSIONS),
    )


@router.post(
    "",
    response_model=ImportRunOut,
    status_code=201,
    summary="Datei hochladen und analysieren (noch ohne Änderungen an der Bibliothek)",
    dependencies=[Depends(_limit_upload)],
)
def upload(
    request: Request,
    file: UploadFile = File(..., description=".csv, .tsv, .txt oder .json"),
    format: str | None = Form(None, description="Format erzwingen, sonst automatisch"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> ImportRunOut:
    settings = _settings(request)
    data = file.file.read(settings.import_max_bytes + 1)
    try:
        run = service.analyze(
            db,
            filename=file.filename,
            data=data,
            principal=principal,
            max_bytes=settings.import_max_bytes,
            max_rows=settings.import_max_rows,
            forced_format=format or None,
        )
    except AppError:
        db.commit()  # abgelehnte Datei trotzdem protokollieren
        raise
    db.commit()
    return run_out(run)


@router.get("", response_model=Page[ImportRunOut], summary="Importprotokoll")
def list_runs(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[ImportRunOut]:
    total = db.scalar(select(func.count()).select_from(ImportRun)) or 0
    runs = db.scalars(
        select(ImportRun).order_by(ImportRun.created_at.desc()).limit(limit).offset(offset)
    ).all()
    return Page[ImportRunOut](
        items=[run_out(r) for r in runs], total=total, limit=limit, offset=offset
    )


@router.get("/{run_id}", response_model=ImportRunOut, summary="Importlauf")
def get_run(
    run_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> ImportRunOut:
    return run_out(service.get_run(db, run_id))


@router.get("/{run_id}/items", response_model=Page[ImportItemOut], summary="Datensätze (Vorschau)")
def list_items(
    run_id: uuid.UUID,
    status: list[ImportItemStatus] = Query([], description="Nur diese Status"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[ImportItemOut]:
    service.get_run(db, run_id)
    stmt = select(ImportItem).where(ImportItem.run_id == run_id)
    if status:
        stmt = stmt.where(ImportItem.status.in_([s.value for s in status]))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    items = list(db.scalars(stmt.order_by(ImportItem.seq).limit(limit).offset(offset)))
    return Page[ImportItemOut](items=_items_out(db, items), total=total, limit=limit, offset=offset)


@router.patch(
    "/{run_id}/items/{item_id}", response_model=ImportItemOut, summary="Entscheidung ändern"
)
def update_item(
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ImportDecisionUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> ImportItemOut:
    item = service.set_item_decision(db, run_id, item_id, body.decision, body.match_track_id)
    db.commit()
    db.refresh(item)
    return _items_out(db, [item])[0]


@router.patch("/{run_id}", response_model=ImportRunOut, summary="Playlist-Optionen ändern")
def update_options(
    run_id: uuid.UUID,
    body: ImportOptionsUpdate,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_write),
) -> ImportRunOut:
    run = service.set_options(
        db,
        run_id,
        playlist_modes={p.name: p.mode for p in body.playlists},
        unassigned=body.unassigned.model_dump(exclude_unset=True) if body.unassigned else None,
    )
    db.commit()
    return run_out(run)


@router.post("/{run_id}/commit", response_model=ImportRunOut, summary="Import bestätigen")
def commit(
    run_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> ImportRunOut:
    run = service.commit(db, run_id)
    # Neue Tracks könnten Duplikate vorhandener sein: Matching-Vorschläge im Hintergrund
    matching_service.request_scan(db)
    db.commit()
    return run_out(run)


@router.post("/{run_id}/discard", response_model=ImportRunOut, summary="Import verwerfen")
def discard(
    run_id: uuid.UUID, db: Session = Depends(get_db), _: Principal = Depends(require_write)
) -> ImportRunOut:
    run = service.discard(db, run_id)
    db.commit()
    return run_out(run)


@router.get(
    "/{run_id}/report",
    summary="Importbericht herunterladen",
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
def report(
    run_id: uuid.UUID,
    format: str = Query("csv", pattern="^(csv|json)$"),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Response:
    run = service.get_run(db, run_id)
    rows = service.report_rows(db, run)
    base = f"importbericht-{run.created_at:%Y-%m-%d-%H%M}"
    if format == "json":
        body = json.dumps(
            {"import": run_out(run).model_dump(mode="json"), "items": rows},
            ensure_ascii=False,
            indent=2,
            default=str,
        ).encode()
        media, filename = "application/json", f"{base}.json"
    else:
        body = to_csv(rows, service.REPORT_COLUMNS)
        media, filename = "text/csv; charset=utf-8", f"{base}.csv"
    return Response(
        body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
