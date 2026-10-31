"""Importlauf: hochladen → analysieren → Entscheidungen → bestätigen → Bericht.

Datenbankregeln:
* Analyse und Commit laufen jeweils in einer Transaktion (Commit durch die Route).
* Im Commit läuft jeder Datensatz in einem eigenen SAVEPOINT. Scheitert er, wird nur
  dieser Datensatz zurückgerollt und als ``failed`` protokolliert – der Rest bleibt.
* Der Importlauf wird beim Commit gesperrt (``FOR UPDATE``) und muss im Status
  ``analyzed`` sein; doppeltes Bestätigen ist dadurch ausgeschlossen.
* Vor dem Anlegen wird erneut gegen die aktuelle Bibliothek abgeglichen, damit in der
  Zwischenzeit angelegte Tracks nicht doppelt entstehen.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from musichub.auth import Principal
from musichub.core.errors import (
    ConflictError,
    NotFoundError,
    PayloadTooLargeError,
    UnprocessableError,
)
from musichub.domain.types import (
    ImportDecision,
    ImportItemStatus,
    ImportResult,
    ImportStatus,
    LinkStatus,
    MatchMethod,
    PlaylistImportMode,
)
from musichub.library import playlists as playlist_service
from musichub.models import ImportItem, ImportRun, Playlist, PlaylistItem, Tag, Track, TrackSource
from musichub.transfer.decoding import (
    UploadError,
    UploadTooLargeError,
    decode_upload,
    safe_filename,
)
from musichub.transfer.formats import FORMATS, default_playlist_name, detect_and_parse
from musichub.transfer.matching import (
    SUGGEST_EXISTING_THRESHOLD,
    LibraryIndex,
    LibraryTrack,
    file_identity_keys,
)
from musichub.transfer.records import FormatError, ImportRecord, ParseResult

log = logging.getLogger(__name__)

#: Formate ohne eigene Playlist-Angabe, die typischerweise eine Playlist sind
SINGLE_LIST_FORMATS = {"txt", "soundiiz_csv", "exportify_csv", "tunemymusic_csv"}


# --- Bibliothek laden -----------------------------------------------------------------


def load_index(db: Session) -> LibraryIndex:
    sources: dict[uuid.UUID, set[str]] = {}
    for track_id, platform, external_id in db.execute(
        select(TrackSource.track_id, TrackSource.platform, TrackSource.external_id).where(
            TrackSource.link_status == LinkStatus.LINKED.value
        )
    ):
        sources.setdefault(track_id, set()).add(f"{platform}:{external_id}")
    rows = db.execute(
        select(
            Track.id, Track.title, Track.artist, Track.version, Track.duration_ms, Track.isrc
        ).order_by(Track.created_at, Track.id)
    )
    return LibraryIndex(
        [
            LibraryTrack(
                id=str(r.id),
                title=r.title,
                artist=r.artist,
                version=r.version,
                duration_ms=r.duration_ms,
                isrc=r.isrc,
                sources=sources.get(r.id, set()),
            )
            for r in rows
        ]
    )


# --- Analyse --------------------------------------------------------------------------


def _fail_run(
    db: Session, *, filename: str, data: bytes, principal: Principal, error: str
) -> ImportRun:
    run = ImportRun(
        filename=safe_filename(filename),
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        status=ImportStatus.FAILED.value,
        created_by=principal.subject,
        error=error,
        options={},
        summary={},
        warnings=[],
    )
    db.add(run)
    db.flush()
    return run


def analyze(
    db: Session,
    *,
    filename: str | None,
    data: bytes,
    principal: Principal,
    max_bytes: int,
    max_rows: int,
    forced_format: str | None = None,
) -> ImportRun:
    """Datei prüfen, einlesen, abgleichen und als Importlauf speichern.

    Ist die Datei als Ganzes unbrauchbar, wird ein Lauf mit Status ``failed``
    protokolliert und ``UnprocessableError`` (mit ``import_id``) geworfen.
    """
    try:
        decoded = decode_upload(filename, data, max_bytes=max_bytes)
        parsed = detect_and_parse(decoded, max_rows=max_rows, forced_format=forced_format)
    except (UploadError, FormatError) as exc:
        run = _fail_run(db, filename=filename or "", data=data, principal=principal, error=str(exc))
        error_cls = (
            PayloadTooLargeError if isinstance(exc, UploadTooLargeError) else UnprocessableError
        )
        raise error_cls(str(exc), extra={"import_id": str(run.id)}) from exc
    return analyze_parsed(
        db,
        parsed,
        filename=decoded.filename,
        sha256=decoded.sha256,
        size_bytes=decoded.size,
        created_by=principal.subject,
    )


def analyze_parsed(
    db: Session,
    parsed: ParseResult,
    *,
    filename: str,
    sha256: str,
    size_bytes: int,
    created_by: str,
) -> ImportRun:
    """Abgleich und Importlauf für bereits gelesene Datensätze (Datei oder Plattform-API)."""
    index = load_index(db)
    records = parsed.records

    # Playlists der Datei: welche Datensätze gehören wohin?
    memberships: dict[int, list[str]] = {}
    for playlist in parsed.playlists:
        for entry in playlist.entries:
            names = memberships.setdefault(entry, [])
            if playlist.name not in names:
                names.append(playlist.name)

    items: list[ImportItem] = []
    first_seen: dict[str, int] = {}
    for seq, record in enumerate(records):
        item = ImportItem(
            seq=seq,
            row=record.row,
            data=record.to_dict(),
            playlists=memberships.get(seq, []),
        )
        if not record.valid:
            item.status, item.decision = ImportItemStatus.INVALID.value, ImportDecision.SKIP.value
        else:
            keys = file_identity_keys(record)
            earlier = next((first_seen[k] for k in keys if k in first_seen), None)
            for k in keys:
                first_seen.setdefault(k, seq if earlier is None else earlier)
            if earlier is not None:
                canonical = items[earlier]
                item.status = ImportItemStatus.FILE_DUPLICATE.value
                item.duplicate_of_seq = earlier
                item.decision = canonical.decision
                item.match_track_id = canonical.match_track_id
                item.match_reason, item.match_score = canonical.match_reason, canonical.match_score
            elif match := index.exact(record):
                item.status = ImportItemStatus.EXISTING.value
                item.decision = ImportDecision.USE_EXISTING.value
                item.match_track_id = uuid.UUID(match.track_id)
                item.match_reason, item.match_score = match.reason, match.score
            elif match := index.similar(record):
                item.status = ImportItemStatus.POTENTIAL_DUPLICATE.value
                item.match_track_id = uuid.UUID(match.track_id)
                item.match_reason, item.match_score = match.reason, round(match.score, 3)
                item.decision = (
                    ImportDecision.USE_EXISTING.value
                    if match.score >= SUGGEST_EXISTING_THRESHOLD
                    else ImportDecision.CREATE.value
                )
            else:
                item.status, item.decision = ImportItemStatus.NEW.value, ImportDecision.CREATE.value
        items.append(item)

    # Playlist-Vorschläge: gleichnamige vorhandene Playlist -> zusammenführen
    existing = {
        name.casefold(): (pid, name)
        for pid, name in db.execute(
            select(Playlist.id, Playlist.name).order_by(Playlist.created_at)
        )
    }
    playlists = []
    for playlist in parsed.playlists:
        match = existing.get(playlist.name.casefold())
        playlists.append(
            {
                "name": playlist.name,
                "description": playlist.description,
                "entries": playlist.entries,
                "existing_playlist_id": str(match[0]) if match else None,
                "mode": (PlaylistImportMode.MERGE if match else PlaylistImportMode.CREATE).value,
            }
        )
    assigned = {e for p in parsed.playlists for e in p.entries}
    unassigned = [
        i.seq for i in items if i.seq not in assigned and i.status != ImportItemStatus.INVALID
    ]

    warnings = list(parsed.warnings)
    previous = db.scalar(
        select(ImportRun)
        .where(
            ImportRun.sha256 == sha256,
            ImportRun.status == ImportStatus.COMMITTED.value,
        )
        .order_by(ImportRun.committed_at.desc())
        .limit(1)
    )
    if previous:
        warnings.insert(
            0,
            f"Diese Datei wurde bereits am {previous.committed_at:%d.%m.%Y} importiert. "
            "Bereits vorhandene Tracks werden erkannt und nicht doppelt angelegt.",
        )

    run = ImportRun(
        filename=filename,
        size_bytes=size_bytes,
        sha256=sha256,
        format=parsed.format,
        status=ImportStatus.ANALYZED.value,
        created_by=created_by,
        warnings=warnings,
        options={
            "playlists": playlists,
            "unassigned": {
                "count": len(unassigned),
                "enabled": bool(unassigned) and parsed.format in SINGLE_LIST_FORMATS,
                "name": default_playlist_name(filename),
            },
        },
        summary={},
    )
    run.items = items
    db.add(run)
    db.flush()
    run.summary = analysis_summary(db, run.id)
    log.info(
        "Import analysiert: %s (%s, %d Datensätze)",
        run.filename,
        run.format,
        len(items),
        extra={"import_id": str(run.id)},
    )
    return run


def analysis_summary(db: Session, run_id: uuid.UUID) -> dict[str, int]:
    counts = dict(
        db.execute(
            select(ImportItem.status, func.count())
            .where(ImportItem.run_id == run_id)
            .group_by(ImportItem.status)
        ).all()
    )
    summary = {s.value: int(counts.get(s.value, 0)) for s in ImportItemStatus}
    summary["total"] = sum(summary.values())
    return summary


# --- Laufen lassen / ändern ---------------------------------------------------------


def get_run(db: Session, run_id: uuid.UUID, *, lock: bool = False) -> ImportRun:
    stmt = select(ImportRun).where(ImportRun.id == run_id)
    if lock:
        stmt = stmt.with_for_update()
    run = db.scalar(stmt)
    if run is None:
        raise NotFoundError(f"Import {run_id} nicht gefunden.")
    return run


def _require_analyzed(run: ImportRun) -> None:
    if run.status != ImportStatus.ANALYZED.value:
        raise ConflictError(
            f"Import ist bereits abgeschlossen (Status {run.status}).",
            extra={"import_status": run.status},
        )


def set_item_decision(
    db: Session,
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    decision: ImportDecision,
    match_track_id: uuid.UUID | None = None,
) -> ImportItem:
    run = get_run(db, run_id, lock=True)
    _require_analyzed(run)
    item = db.get(ImportItem, item_id)
    if item is None or item.run_id != run_id:
        raise NotFoundError(f"Datensatz {item_id} nicht gefunden.")
    if item.status == ImportItemStatus.INVALID.value and decision is not ImportDecision.SKIP:
        raise UnprocessableError("Fehlerhafte Datensätze können nicht importiert werden.")
    if item.status == ImportItemStatus.FILE_DUPLICATE.value:
        raise UnprocessableError(
            "Doppelte Einträge folgen der Entscheidung für ihr erstes Vorkommen in der Datei."
        )
    if decision is ImportDecision.USE_EXISTING:
        target = match_track_id or item.match_track_id
        if target is None or db.get(Track, target) is None:
            raise UnprocessableError("Für „vorhandenen Track verwenden“ fehlt ein gültiger Track.")
        item.match_track_id = target
    item.decision = decision.value
    # Spätere Vorkommen in der Datei folgen
    db.execute(
        update(ImportItem)
        .where(ImportItem.run_id == run_id, ImportItem.duplicate_of_seq == item.seq)
        .values(decision=item.decision, match_track_id=item.match_track_id)
    )
    db.flush()
    return item


def set_options(
    db: Session,
    run_id: uuid.UUID,
    *,
    playlist_modes: dict[str, PlaylistImportMode] | None = None,
    unassigned: dict[str, Any] | None = None,
) -> ImportRun:
    run = get_run(db, run_id, lock=True)
    _require_analyzed(run)
    options = dict(run.options)
    if playlist_modes:
        known = {p["name"] for p in options.get("playlists", [])}
        unknown = set(playlist_modes) - known
        if unknown:
            raise UnprocessableError(
                "Unbekannte Playlist im Import.", extra={"names": sorted(unknown)}
            )
        playlists = []
        for p in options.get("playlists", []):
            mode = playlist_modes.get(p["name"])
            if mode is not None:
                if mode in (PlaylistImportMode.MERGE, PlaylistImportMode.REPLACE) and not p.get(
                    "existing_playlist_id"
                ):
                    raise UnprocessableError(
                        f"„{p['name']}“ gibt es noch nicht – nur anlegen oder überspringen möglich."
                    )
                p = {**p, "mode": mode.value}
            playlists.append(p)
        options["playlists"] = playlists
    if unassigned is not None:
        current = dict(options.get("unassigned", {}))
        current.update({k: v for k, v in unassigned.items() if v is not None})
        options["unassigned"] = current
    run.options = options
    db.flush()
    return run


def discard(db: Session, run_id: uuid.UUID) -> ImportRun:
    run = get_run(db, run_id, lock=True)
    _require_analyzed(run)
    run.status = ImportStatus.DISCARDED.value
    db.flush()
    return run


# --- Commit ---------------------------------------------------------------------------


class _Committer:
    def __init__(self, db: Session) -> None:
        self.db = db
        self.index = load_index(db)
        self.tags: dict[tuple[str, str], Tag] = {
            (t.category.casefold(), t.name.casefold()): t for t in db.scalars(select(Tag))
        }
        self.tags_created = 0

    def tag(self, raw: str) -> Tag:
        category, _, name = raw.partition(":") if ":" in raw else ("Allgemein", "", raw)
        category, name = category.strip()[:50] or "Allgemein", name.strip()[:100]
        key = (category.casefold(), name.casefold())
        if key not in self.tags:
            tag = Tag(name=name, category=category)
            self.db.add(tag)
            self.db.flush()
            self.tags[key] = tag
            self.tags_created += 1
        return self.tags[key]

    def attach(self, track: Track, record: ImportRecord, *, fill: bool) -> list[str]:
        """Quellen, Tags und – bei ``fill`` – leere Metadaten ergänzen. Lokales bleibt."""
        notes: list[str] = []
        if fill:
            for name in ("album", "version", "duration_ms", "isrc", "release_date"):
                value = getattr(record, name)
                if value and getattr(track, name) is None:
                    if name == "release_date":
                        value = date.fromisoformat(value)
                    if name == "isrc" and self.index.by_isrc.get(value, str(track.id)) != str(
                        track.id
                    ):
                        continue
                    setattr(track, name, value)
        linked_platforms = {
            s.platform for s in track.sources if s.link_status == LinkStatus.LINKED.value
        }
        known = {(s.platform, s.external_id) for s in track.sources}
        for src in record.sources:
            key = f"{src.platform}:{src.external_id}"
            if (src.platform, src.external_id) in known:
                continue
            owner = self.index.by_source.get(key)
            if owner and owner != str(track.id):
                notes.append(f"{src.platform}-Quelle gehört bereits zu einem anderen Track.")
                continue
            status = LinkStatus.LINKED
            if src.platform in linked_platforms:
                status = LinkStatus.CANDIDATE
                notes.append(f"Weitere {src.platform}-Quelle als Vorschlag gespeichert.")
            source = TrackSource(
                track_id=track.id,
                platform=src.platform,
                external_id=src.external_id,
                url=src.url,
                link_status=status.value,
                match_method=MatchMethod.IMPORT.value,
            )
            self.db.add(source)
            track.sources.append(source)
            known.add((src.platform, src.external_id))
            if status is LinkStatus.LINKED:
                linked_platforms.add(src.platform)
                self.index.by_source[key] = str(track.id)
        existing_tags = {t.id for t in track.tags}
        for raw in record.tags:
            tag = self.tag(raw)
            if tag.id not in existing_tags:
                track.tags.append(tag)
                existing_tags.add(tag.id)
        self.db.flush()
        return notes

    def create(self, record: ImportRecord) -> Track:
        track = Track(
            title=record.title,
            artist=record.artist,
            album=record.album,
            version=record.version,
            duration_ms=record.duration_ms,
            isrc=record.isrc,
            release_date=date.fromisoformat(record.release_date) if record.release_date else None,
            notes=record.notes,
        )
        self.db.add(track)
        self.db.flush()
        self.attach(track, record, fill=False)
        self.index.add(
            LibraryTrack(
                id=str(track.id),
                title=track.title,
                artist=track.artist,
                version=track.version,
                duration_ms=track.duration_ms,
                isrc=track.isrc,
                sources={
                    f"{s.platform}:{s.external_id}"
                    for s in track.sources
                    if s.link_status == "linked"
                },
            )
        )
        return track


def commit(db: Session, run_id: uuid.UUID) -> ImportRun:
    run = get_run(db, run_id, lock=True)
    _require_analyzed(run)
    committer = _Committer(db)
    items = list(
        db.scalars(select(ImportItem).where(ImportItem.run_id == run_id).order_by(ImportItem.seq))
    )
    by_seq = {i.seq: i for i in items}

    for item in items:
        if item.status == ImportItemStatus.FILE_DUPLICATE.value:
            continue
        if (
            item.status == ImportItemStatus.INVALID.value
            or item.decision == ImportDecision.SKIP.value
        ):
            item.result = ImportResult.SKIPPED.value
            continue
        record = ImportRecord.from_dict(item.data)
        try:
            with db.begin_nested():
                track = None
                if item.decision == ImportDecision.USE_EXISTING.value and item.match_track_id:
                    track = db.get(Track, item.match_track_id)
                if track is not None:
                    notes = committer.attach(track, record, fill=True)
                    item.result = ImportResult.LINKED.value
                else:
                    hit = committer.index.exact(record)
                    if hit:  # in der Zwischenzeit angelegt (oder Treffer gelöscht)
                        track = db.get(Track, uuid.UUID(hit.track_id))
                    if track is not None:
                        notes = committer.attach(track, record, fill=True)
                        notes.insert(0, "Track war inzwischen vorhanden und wurde verknüpft.")
                        item.result = ImportResult.LINKED.value
                    else:
                        track = committer.create(record)
                        notes = []
                        item.result = ImportResult.CREATED.value
                item.result_track_id = track.id
                item.result_message = " ".join(notes) or None
        except Exception as exc:  # einzelner Datensatz scheitert, Rest läuft weiter
            log.warning("Import-Datensatz Zeile %d fehlgeschlagen: %s", item.row, exc)
            item.result = ImportResult.FAILED.value
            item.result_track_id = None
            item.result_message = "Datensatz konnte nicht gespeichert werden."

    for item in items:
        if item.status != ImportItemStatus.FILE_DUPLICATE.value:
            continue
        canonical = by_seq.get(item.duplicate_of_seq or -1)
        if canonical is None or canonical.result_track_id is None:
            item.result = ImportResult.SKIPPED.value
            continue
        try:
            with db.begin_nested():
                track = db.get(Track, canonical.result_track_id)
                assert track is not None
                notes = committer.attach(track, ImportRecord.from_dict(item.data), fill=True)
            item.result = ImportResult.LINKED.value
            item.result_track_id = canonical.result_track_id
            item.result_message = " ".join(["Steht mehrfach in der Datei.", *notes])
        except Exception as exc:
            log.warning("Import-Duplikat Zeile %d fehlgeschlagen: %s", item.row, exc)
            item.result = ImportResult.FAILED.value
            item.result_message = "Datensatz konnte nicht gespeichert werden."
    db.flush()

    playlist_results = _apply_playlists(db, run, by_seq)

    counts = Counter(i.result for i in items)
    run.summary = {
        **analysis_summary(db, run.id),
        "created": counts.get(ImportResult.CREATED.value, 0),
        "linked": counts.get(ImportResult.LINKED.value, 0),
        "skipped": counts.get(ImportResult.SKIPPED.value, 0),
        "failed": counts.get(ImportResult.FAILED.value, 0),
        "tags_created": committer.tags_created,
        "playlists": playlist_results,
    }
    run.status = ImportStatus.COMMITTED.value
    run.committed_at = datetime.now(UTC)
    db.flush()
    log.info(
        "Import bestätigt: %s (%d neu, %d verknüpft, %d fehlgeschlagen)",
        run.filename,
        run.summary["created"],
        run.summary["linked"],
        run.summary["failed"],
        extra={"import_id": str(run.id)},
    )
    return run


def _apply_playlists(
    db: Session, run: ImportRun, by_seq: dict[int, ImportItem]
) -> list[dict[str, Any]]:
    plans: list[dict[str, Any]] = list(run.options.get("playlists", []))
    unassigned = run.options.get("unassigned", {})
    if unassigned.get("enabled") and str(unassigned.get("name", "")).strip():
        assigned = {e for p in plans for e in p["entries"]}
        plans.append(
            {
                "name": str(unassigned["name"]).strip()[:300],
                "description": None,
                "entries": [s for s in sorted(by_seq) if s not in assigned],
                "existing_playlist_id": None,
                "mode": PlaylistImportMode.CREATE.value,
            }
        )

    results: list[dict[str, Any]] = []
    for plan in plans:
        mode = PlaylistImportMode(plan["mode"])
        track_ids = [
            by_seq[s].result_track_id
            for s in plan["entries"]
            if s in by_seq and by_seq[s].result_track_id is not None
        ]
        result = {"name": plan["name"], "mode": mode.value, "added": 0, "playlist_id": None}
        if mode is PlaylistImportMode.SKIP:
            results.append(result)
            continue
        target_id = plan.get("existing_playlist_id")
        target = db.get(Playlist, uuid.UUID(target_id)) if target_id else None
        try:
            with db.begin_nested():
                if mode is PlaylistImportMode.CREATE or target is None:
                    target = Playlist(name=plan["name"][:300], description=plan.get("description"))
                    db.add(target)
                    db.flush()
                    added = (
                        playlist_service.add_items(db, target.id, track_ids) if track_ids else []
                    )
                elif mode is PlaylistImportMode.REPLACE:
                    playlist_service.lock_playlist(db, target.id)
                    db.execute(delete(PlaylistItem).where(PlaylistItem.playlist_id == target.id))
                    db.flush()
                    added = (
                        playlist_service.add_items(db, target.id, track_ids) if track_ids else []
                    )
                else:  # MERGE: nur ergänzen, was noch fehlt – Wiederholung erzeugt nichts Neues
                    added = (
                        playlist_service.add_items(db, target.id, track_ids, skip_existing=True)
                        if track_ids
                        else []
                    )
            result.update(added=len(added), playlist_id=str(target.id))
        except Exception as exc:
            log.warning("Playlist „%s“ beim Import fehlgeschlagen: %s", plan["name"], exc)
            result["error"] = "Playlist konnte nicht gespeichert werden."
        results.append(result)
    return results


# --- Bericht ---------------------------------------------------------------------------

REPORT_COLUMNS = [
    "zeile",
    "status",
    "entscheidung",
    "ergebnis",
    "titel",
    "artist",
    "version",
    "album",
    "isrc",
    "quellen",
    "playlists",
    "abgleich",
    "aehnlichkeit",
    "vorhandener_track",
    "musichub_track",
    "fehler",
    "hinweise",
]


def report_rows(db: Session, run: ImportRun) -> list[dict[str, Any]]:
    items: Sequence[ImportItem] = db.scalars(
        select(ImportItem).where(ImportItem.run_id == run.id).order_by(ImportItem.seq)
    ).all()
    rows = []
    for i in items:
        d = i.data
        hints = list(d.get("warnings", []))
        if i.result_message:
            hints.append(i.result_message)
        rows.append(
            {
                "zeile": i.row,
                "status": i.status,
                "entscheidung": i.decision,
                "ergebnis": i.result or "",
                "titel": d.get("title") or "",
                "artist": d.get("artist") or "",
                "version": d.get("version") or "",
                "album": d.get("album") or "",
                "isrc": d.get("isrc") or "",
                "quellen": " ".join(s["url"] for s in d.get("sources", [])),
                "playlists": " | ".join(i.playlists),
                "abgleich": i.match_reason or "",
                "aehnlichkeit": i.match_score if i.match_score is not None else "",
                "vorhandener_track": str(i.match_track_id or ""),
                "musichub_track": str(i.result_track_id or ""),
                "fehler": " ".join(d.get("errors", [])),
                "hinweise": " ".join(hints),
            }
        )
    return rows


#: Importe über Plattform-APIs (connections/importer.py) – keine Dateiformate
API_FORMATS = {
    "spotify_api": "Spotify (verbundenes Konto)",
    "youtube_api": "YouTube (verbundenes Konto)",
    "soundcloud_api": "SoundCloud (verbundenes Konto)",
}


def format_label(fmt: str | None) -> str | None:
    if fmt in API_FORMATS:
        return API_FORMATS[fmt]
    return FORMATS[fmt].label if fmt in FORMATS else fmt
