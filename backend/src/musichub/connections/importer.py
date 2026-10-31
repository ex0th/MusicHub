"""Hintergrundjobs für verbundene Konten – ausschließlich lesend gegenüber den Plattformen.

* ``platform.refresh_playlists`` – Playlist-Liste des Kontos aktualisieren (Metadaten,
  Versionsmerkmal). Mit ``import_changed`` werden danach geänderte, bereits importierte
  Playlists erneut importiert.
* ``platform.import_playlist`` – eine Playlist lesen und über den normalen Importlauf
  (``transfer/service``) in die Bibliothek übernehmen. Inkrementell: unveränderte
  Playlists (gleiches Versionsmerkmal) werden übersprungen.
* ``platform.refresh_metadata`` – Metadaten und Verfügbarkeit verknüpfter Tracks
  nachladen; ergänzt nur leere Felder (lokal führt).

Regeln beim Import: Vorhandene Tracks werden über Plattform-ID, ISRC und Metadaten
erkannt und nur ergänzt. Mögliche Duplikate werden **neu angelegt** statt automatisch
verknüpft – die Matching-Seite schlägt sie danach zum Zusammenführen vor. Eine bereits
importierte Playlist wird nur ergänzt (``merge``), lokal entfernte oder umsortierte Einträge
bleiben, wie sie sind.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from musichub.connections import service
from musichub.connectors.base import ConnectorError, RemoteAccount, RemoteTrack
from musichub.core.config import get_settings
from musichub.db.session import get_sessionmaker
from musichub.domain.platform_urls import PlatformUrlError, canonical_url, validate_external_id
from musichub.domain.types import (
    Availability,
    ImportDecision,
    ImportItemStatus,
    LinkStatus,
    Platform,
    PlaylistImportMode,
    RemoteImportStatus,
)
from musichub.jobs.handlers import JobContext, JobError, handler
from musichub.models import ImportItem, Playlist, RemotePlaylist, Track, TrackSource
from musichub.transfer import service as transfer
from musichub.transfer.records import (
    ImportRecord,
    ParsedPlaylist,
    ParseResult,
    SourceRef,
    parse_isrc,
)

log = logging.getLogger(__name__)

#: Höchstzahl Tracks je Metadaten-Lauf (Spotify: ein Aufruf pro Track im Development Mode)
METADATA_BATCH = {Platform.SPOTIFY: 100, Platform.YOUTUBE: 500, Platform.SOUNDCLOUD: 0}


def _now() -> datetime:
    return datetime.now(UTC)


def _platform(ctx: JobContext) -> Platform:
    try:
        return Platform(ctx.payload["platform"])
    except (KeyError, ValueError):
        raise JobError("Job ohne gültige Plattform.", retryable=False) from None


def _translate(exc: ConnectorError) -> JobError:
    return JobError(exc.message, retryable=exc.retryable, retry_after=exc.retry_after)


# --- Playlist-Liste ----------------------------------------------------------------------------


@handler(service.JOB_REFRESH_PLAYLISTS)
def refresh_playlists(ctx: JobContext) -> None:
    platform = _platform(ctx)
    db = ctx.session
    settings = get_settings()
    account = service.require_account(db, platform)
    remote_account = RemoteAccount(account.external_user_id, account.display_name)
    ctx.progress(0, None, "Playlists werden abgerufen …")
    try:
        playlists = service.call_with_token(
            settings,
            ctx.connectors,
            platform,
            lambda c, token: c.list_playlists(token, remote_account),
        )
    except ConnectorError as exc:
        raise _translate(exc) from None

    now = _now()
    existing = {
        rp.external_id: rp
        for rp in db.scalars(
            select(RemotePlaylist).where(RemotePlaylist.platform == platform.value)
        )
    }
    seen: set[str] = set()
    for info in playlists:
        seen.add(info.external_id)
        rp = existing.get(info.external_id)
        if rp is None:
            rp = RemotePlaylist(platform=platform.value, external_id=info.external_id)
            db.add(rp)
        rp.name = info.name[:500]
        rp.owner_name = (info.owner_name or "")[:255] or None
        rp.description = info.description
        rp.track_count = info.track_count
        rp.url = info.url
        rp.owned = info.owned
        rp.items_accessible = info.items_accessible
        rp.remote_version = (info.version or "")[:255] or None
        rp.seen_at = now
        rp.removed_at = None
    removed = 0
    for external_id, rp in existing.items():
        if external_id not in seen and rp.removed_at is None:
            rp.removed_at = now
            removed += 1
    account.playlists_synced_at = now
    db.flush()

    queued = 0
    if ctx.payload.get("import_changed"):
        changed = db.scalars(
            select(RemotePlaylist).where(
                RemotePlaylist.platform == platform.value,
                RemotePlaylist.local_playlist_id.is_not(None),
                RemotePlaylist.removed_at.is_(None),
                RemotePlaylist.items_accessible.is_(True),
                RemotePlaylist.imported_version.is_distinct_from(RemotePlaylist.remote_version),
            )
        ).all()
        for rp in changed:
            service.enqueue_unique(
                db,
                service.JOB_IMPORT_PLAYLIST,
                {"platform": platform.value, "remote_playlist_id": str(rp.id)},
                max_attempts=6,
            )
            queued += 1
    ctx.progress(len(playlists), len(playlists), "Playlist-Liste aktualisiert")
    ctx.result.update(playlists=len(playlists), removed=removed, imports_queued=queued)


# --- Playlist importieren ---------------------------------------------------------------------


def _record(row: int, track: RemoteTrack) -> ImportRecord:
    record = ImportRecord(
        row=row,
        title=track.title or None,
        artist=", ".join(a for a in track.artists if a) or None,
        album=track.album,
        duration_ms=track.duration_ms,
        release_date=track.release_date,
    )
    if track.isrc:
        record.isrc = parse_isrc(track.isrc, record)
    if track.external_id:
        try:
            external_id = validate_external_id(track.platform, track.external_id)
        except PlatformUrlError:
            record.warnings.append("Plattform-ID nicht erkannt, ohne Link übernommen.")
        else:
            record.sources.append(
                SourceRef(
                    track.platform.value,
                    external_id,
                    canonical_url(track.platform, external_id),
                )
            )
    if track.note:
        record.warnings.append(track.note)
    record.raw = {
        "title": track.title,
        "artist": ", ".join(track.artists),
        "id": track.external_id,
    }
    return record.finalize()


def _record_failure(remote_id: uuid.UUID, message: str) -> None:
    """Fehler sichtbar machen, obwohl die Job-Transaktion zurückgerollt wird."""
    with get_sessionmaker().begin() as s:
        rp = s.get(RemotePlaylist, remote_id)
        if rp is not None:
            rp.last_import_status = RemoteImportStatus.ERROR.value
            rp.last_error = message[:2000]


@handler(service.JOB_IMPORT_PLAYLIST)
def import_playlist(ctx: JobContext) -> None:
    platform = _platform(ctx)
    try:
        remote_id = uuid.UUID(str(ctx.payload["remote_playlist_id"]))
    except (KeyError, ValueError):
        raise JobError("Job ohne gültige Playlist.", retryable=False) from None
    try:
        _import(ctx, platform, remote_id)
    except JobError as exc:
        _record_failure(remote_id, str(exc))
        raise
    except ConnectorError as exc:
        _record_failure(remote_id, exc.message)
        raise _translate(exc) from None


def _import(ctx: JobContext, platform: Platform, remote_id: uuid.UUID) -> None:
    db = ctx.session
    settings = get_settings()
    account = service.require_account(db, platform)
    # bewusst ohne Zeilensperre: Fehler werden in eigener Transaktion vermerkt
    rp = db.get(RemotePlaylist, remote_id)
    if rp is None or rp.platform != platform.value:
        raise JobError("Playlist nicht gefunden.", retryable=False)
    if not rp.items_accessible:
        raise JobError(
            "Die Plattform gibt die Inhalte dieser Playlist nicht frei.", retryable=False
        )
    local = db.get(Playlist, rp.local_playlist_id) if rp.local_playlist_id else None
    force = bool(ctx.payload.get("force"))
    if (
        not force
        and local is not None
        and rp.remote_version
        and rp.imported_version == rp.remote_version
        and rp.last_import_status
        in (RemoteImportStatus.OK.value, RemoteImportStatus.UNCHANGED.value)
    ):
        rp.last_import_status = RemoteImportStatus.UNCHANGED.value
        rp.last_error = None
        ctx.result.update(unchanged=True, playlist=rp.name)
        return

    label = service.PLATFORM_LABEL[platform]
    ctx.progress(0, rp.track_count, f"{rp.name}: Tracks werden gelesen …")

    def fetch(c: Any, token: str) -> list[RemoteTrack]:
        collected: list[RemoteTrack] = []
        for page in c.iter_playlist_tracks(token, rp.external_id):
            collected.extend(page.tracks)
            ctx.progress(len(collected), page.total or rp.track_count, f"{rp.name}: Tracks lesen")
        return collected

    tracks = service.call_with_token(settings, ctx.connectors, platform, fetch)

    skipped: Counter[str] = Counter()
    records: list[ImportRecord] = []
    for track in tracks:
        if not track.available or not track.title:
            skipped[track.note or "nicht verfügbar"] += 1
            continue
        records.append(_record(len(records) + 1, track))
    warnings = [f"{n}× übersprungen: {reason}" for reason, n in skipped.items()]
    parsed = ParseResult(
        format=f"{platform.value}_api",
        records=records,
        playlists=[
            ParsedPlaylist(
                name=rp.name[:300], description=rp.description, entries=list(range(len(records)))
            )
        ],
        warnings=warnings,
    )
    fingerprint = hashlib.sha256(
        f"{platform.value}:{rp.external_id}:{rp.remote_version}:{_now().isoformat()}".encode()
    ).hexdigest()
    ctx.progress(len(tracks), len(tracks), f"{rp.name}: Abgleich mit der Bibliothek")
    run = transfer.analyze_parsed(
        db,
        parsed,
        filename=f"{label}: {rp.name}"[:200],
        sha256=fingerprint,
        size_bytes=0,
        created_by=account.connected_by,
    )
    # Konservativ: mögliche Duplikate nicht automatisch verknüpfen (→ Matching-Vorschläge)
    db.execute(
        update(ImportItem)
        .where(
            ImportItem.run_id == run.id,
            ImportItem.status == ImportItemStatus.POTENTIAL_DUPLICATE.value,
        )
        .values(decision=ImportDecision.CREATE.value)
    )
    plan = dict(run.options["playlists"][0])
    if local is not None:
        plan.update(mode=PlaylistImportMode.MERGE.value, existing_playlist_id=str(local.id))
    else:
        plan.update(mode=PlaylistImportMode.CREATE.value, existing_playlist_id=None)
    unassigned = {**run.options.get("unassigned", {}), "enabled": False}
    run.options = {**run.options, "playlists": [plan], "unassigned": unassigned}
    db.flush()
    db.expire_all()
    run = transfer.commit(db, run.id)

    results = run.summary.get("playlists") or []
    playlist_id = results[0].get("playlist_id") if results else None
    rp = db.get(RemotePlaylist, remote_id)
    assert rp is not None
    if playlist_id:
        rp.local_playlist_id = uuid.UUID(playlist_id)
    now = _now()
    rp.imported_version = rp.remote_version
    rp.last_imported_at = now
    rp.last_import_status = RemoteImportStatus.OK.value
    rp.last_import_run_id = run.id
    rp.last_error = None
    account = service.require_account(db, platform)
    account.last_import_at = now

    _update_availability(db, platform, tracks, now)
    from musichub.library.matching import request_scan

    request_scan(db)
    db.flush()
    ctx.result.update(
        playlist=rp.name,
        import_id=str(run.id),
        tracks=len(tracks),
        created=run.summary.get("created", 0),
        linked=run.summary.get("linked", 0),
        failed=run.summary.get("failed", 0),
        skipped=sum(skipped.values()),
        added_to_playlist=results[0].get("added", 0) if results else 0,
    )
    log.info(
        "Playlist importiert",
        extra={"platform": platform.value, "import_id": str(run.id), "tracks": len(tracks)},
    )


def _update_availability(
    db: Session, platform: Platform, tracks: list[RemoteTrack], now: datetime
) -> None:
    """Was die Plattform gerade geliefert hat, ist verfügbar; Gesperrtes nicht."""
    available = {t.external_id for t in tracks if t.external_id and t.available}
    blocked = {t.external_id for t in tracks if t.external_id and not t.available} - available
    for ids, value in ((available, Availability.AVAILABLE), (blocked, Availability.UNAVAILABLE)):
        if ids:
            db.execute(
                update(TrackSource)
                .where(
                    TrackSource.platform == platform.value,
                    TrackSource.external_id.in_(ids),
                    TrackSource.link_status == LinkStatus.LINKED.value,
                )
                .values(availability=value.value, last_checked_at=now)
            )


# --- Metadaten verknüpfter Tracks ----------------------------------------------------------------


@handler(service.JOB_REFRESH_METADATA)
def refresh_metadata(ctx: JobContext) -> None:
    platform = _platform(ctx)
    db = ctx.session
    settings = get_settings()
    service.require_account(db, platform)
    limit = int(ctx.payload.get("limit") or METADATA_BATCH[platform])
    if limit <= 0:
        raise JobError("Für diese Plattform nicht verfügbar.", retryable=False)
    sources = db.scalars(
        select(TrackSource)
        .where(
            TrackSource.platform == platform.value,
            TrackSource.link_status == LinkStatus.LINKED.value,
        )
        .order_by(TrackSource.last_checked_at.asc().nulls_first(), TrackSource.created_at)
        .limit(limit)
    ).all()
    ids = [s.external_id for s in sources]
    ctx.progress(0, len(ids), "Metadaten werden abgerufen …")
    try:
        remote = service.call_with_token(
            settings, ctx.connectors, platform, lambda c, token: c.get_tracks(token, ids)
        )
    except ConnectorError as exc:
        raise _translate(exc) from None

    now = _now()
    counts: Counter[str] = Counter()
    for source in sources:
        data = remote.get(source.external_id)
        source.last_checked_at = now
        if data is None:
            source.availability = Availability.UNAVAILABLE.value
            counts["unavailable"] += 1
            continue
        source.availability = (
            Availability.AVAILABLE if data.available else Availability.UNAVAILABLE
        ).value
        counts["available" if data.available else "unavailable"] += 1
        track = db.get(Track, source.track_id)
        if track is None:
            continue
        filled = _fill(track, data)
        if filled:
            counts["filled"] += 1
    db.flush()
    ctx.progress(len(ids), len(ids), "Metadaten aktualisiert")
    ctx.result.update(checked=len(ids), **counts)


def _fill(track: Track, data: RemoteTrack) -> bool:
    """Nur leere Felder ergänzen – lokale Angaben werden nie überschrieben."""
    changed = False
    record = ImportRecord(row=0)
    values: dict[str, Any] = {
        "duration_ms": data.duration_ms,
        "isrc": parse_isrc(data.isrc, record) if data.isrc else None,
        "album": (data.album or "")[:500] or None,
        "release_date": datetime.fromisoformat(data.release_date).date()
        if data.release_date
        else None,
    }
    for name, value in values.items():
        if value and getattr(track, name) in (None, ""):
            setattr(track, name, value)
            changed = True
    return changed
