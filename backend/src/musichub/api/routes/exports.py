"""Export der Bibliothek und einzelner Playlists (MusicHub-CSV/-JSON)."""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from musichub.auth import Principal, current_principal
from musichub.db.session import get_db
from musichub.library import playlists as playlist_service
from musichub.transfer.export import export_csv, export_filename, export_json

router = APIRouter(prefix="/api/v1", tags=["export"])

FORMAT = Query("json", pattern="^(csv|json)$", description="csv oder json")


def _download(data: bytes, filename: str, kind: str) -> Response:
    media = "text/csv; charset=utf-8" if kind == "csv" else "application/json"
    return Response(
        data,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


def _render(db: Session, kind: str, playlist_id: uuid.UUID | None) -> bytes:
    if kind == "csv":
        return export_csv(db, playlist_id=playlist_id)
    return json.dumps(
        export_json(db, playlist_id=playlist_id), ensure_ascii=False, indent=2
    ).encode()


@router.get(
    "/export/library",
    summary="Gesamte Bibliothek exportieren (Tracks, Plattform-Links, Tags, Playlists)",
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
def export_library(
    format: str = FORMAT,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Response:
    return _download(_render(db, format, None), export_filename(format), format)


@router.get(
    "/playlists/{playlist_id}/export",
    summary="Eine Playlist exportieren",
    responses={200: {"content": {"text/csv": {}, "application/json": {}}}},
)
def export_playlist(
    playlist_id: uuid.UUID,
    format: str = FORMAT,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Response:
    playlist = playlist_service.get_playlist(db, playlist_id)
    return _download(
        _render(db, format, playlist_id),
        export_filename(format, playlist_name=playlist.name),
        format,
    )
