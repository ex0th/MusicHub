"""Export der Bibliothek bzw. einzelner Playlists als MusicHub-CSV und -JSON.

Beide Formate lassen sich ohne Verlust wieder importieren: Jeder Track trägt seine
MusicHub-ID, seine bestätigten Plattform-Links und seine ISRC – beim Wiederimport wird er
darüber erkannt und nicht doppelt angelegt.

CSV-Zellen, die mit ``= + - @`` oder Tab/CR beginnen, werden mit ``'`` versehen, damit
Tabellenprogramme sie nicht als Formel ausführen (CSV/Formula-Injection). Der Import
von MusicHub-CSV entfernt dieses Zeichen wieder.
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from musichub.domain.types import LinkStatus, Platform
from musichub.models import Playlist, PlaylistItem, Track
from musichub.transfer.formats.adapters import MUSICHUB_CSV_COLUMNS
from musichub.transfer.records import FORMULA_PREFIXES

FORMAT_VERSION = 1


def safe_cell(value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def to_csv(rows: Iterable[dict[str, Any]], columns: Sequence[str]) -> bytes:
    """UTF-8 mit BOM (Excel erkennt die Kodierung), Komma, alle Felder abgesichert."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([safe_cell(row.get(c)) for c in columns])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


def slug(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-zA-Z0-9]+", "-", folded).strip("-").lower()[:60] or "playlist"


def _tag_label(category: str, name: str) -> str:
    return f"{category}:{name}"


def _track_json(track: Track) -> dict[str, Any]:
    return {
        "id": str(track.id),
        "title": track.title,
        "artist": track.artist,
        "version": track.version,
        "album": track.album,
        "duration_ms": track.duration_ms,
        "isrc": track.isrc,
        "release_date": track.release_date.isoformat() if track.release_date else None,
        "notes": track.notes,
        "tags": [_tag_label(t.category, t.name) for t in track.tags],
        "sources": [
            {
                "platform": s.platform,
                "external_id": s.external_id,
                "url": s.url,
                "link_status": s.link_status,
                "availability": s.availability,
                "last_checked_at": s.last_checked_at.isoformat() if s.last_checked_at else None,
            }
            for s in track.sources
        ],
        "created_at": track.created_at.isoformat(),
        "updated_at": track.updated_at.isoformat(),
    }


def _load(db: Session, playlist_id: uuid.UUID | None) -> tuple[list[Track], list[Playlist]]:
    playlists_stmt = (
        select(Playlist)
        .options(selectinload(Playlist.items).selectinload(PlaylistItem.track))
        .order_by(Playlist.name, Playlist.id)
    )
    if playlist_id is not None:
        playlists_stmt = playlists_stmt.where(Playlist.id == playlist_id)
    playlists = list(db.scalars(playlists_stmt).unique())
    if playlist_id is not None:
        ids = {i.track_id for p in playlists for i in p.items}
        tracks = list(
            db.scalars(select(Track).where(Track.id.in_(ids)).order_by(Track.artist, Track.title))
        )
    else:
        tracks = list(db.scalars(select(Track).order_by(Track.artist, Track.title, Track.id)))
    return tracks, playlists


def export_json(db: Session, *, playlist_id: uuid.UUID | None = None) -> dict[str, Any]:
    tracks, playlists = _load(db, playlist_id)
    return {
        "format": "musichub",
        "version": FORMAT_VERSION,
        "exported_at": datetime.now(UTC).isoformat(),
        "scope": "playlist" if playlist_id else "library",
        "tracks": [_track_json(t) for t in tracks],
        "playlists": [
            {
                "id": str(p.id),
                "name": p.name,
                "description": p.description,
                "tracks": [str(i.track_id) for i in p.items],
            }
            for p in playlists
        ],
    }


def _csv_row(track: Track, playlist: str = "", position: int | str = "") -> dict[str, Any]:
    links = {
        s.platform: s.url
        for s in track.sources
        if s.link_status == LinkStatus.LINKED.value and s.url
    }
    return {
        "musichub_id": str(track.id),
        "title": track.title,
        "artist": track.artist,
        "version": track.version,
        "album": track.album,
        "duration_ms": track.duration_ms,
        "isrc": track.isrc,
        "release_date": track.release_date.isoformat() if track.release_date else None,
        "tags": "|".join(_tag_label(t.category, t.name) for t in track.tags),
        "spotify_url": links.get(Platform.SPOTIFY.value),
        "soundcloud_url": links.get(Platform.SOUNDCLOUD.value),
        "youtube_url": links.get(Platform.YOUTUBE.value),
        "playlist": playlist,
        "playlist_position": position,
        "notes": track.notes,
    }


def export_csv(db: Session, *, playlist_id: uuid.UUID | None = None) -> bytes:
    """Eine Zeile je Playlist-Eintrag; Tracks ohne Playlist (nur Bibliothek) je eine Zeile."""
    tracks, playlists = _load(db, playlist_id)
    rows: list[dict[str, Any]] = []
    in_playlist: set[uuid.UUID] = set()
    for p in playlists:
        for item in p.items:
            rows.append(_csv_row(item.track, p.name, item.position + 1))
            in_playlist.add(item.track_id)
    if playlist_id is None:
        rows += [_csv_row(t) for t in tracks if t.id not in in_playlist]
    return to_csv(rows, MUSICHUB_CSV_COLUMNS)


def export_filename(kind: str, *, playlist_name: str | None = None) -> str:
    today = datetime.now(UTC).date().isoformat()
    base = f"playlist-{slug(playlist_name)}" if playlist_name else "musichub-bibliothek"
    return f"{base}-{today}.{kind}"
