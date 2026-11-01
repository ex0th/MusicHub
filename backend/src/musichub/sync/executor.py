"""Job ``sync.run``: Vorschau (Dry-Run) und Ausführung einer Synchronisierung.

Ablauf:
1. Lokale Playlist laden.
2. Verfügbare Tracks auf der Zielplattform bestimmen: bestätigte, nicht unverfügbare
   Verknüpfung; sonst bisheriges Suchergebnis; noch nicht geprüfte Tracks werden gesucht
   (begrenzt je Lauf). Nur **sichere** Treffer der Matching-Engine werden verwendet.
3. Zielplaylist einlesen.
4. Differenzen berechnen (``sync/engine.plan_sync``).
5. Vorschau speichern (Dry-Run endet hier).
6. Änderungen ausführen – nur nach allen Sicherheitsprüfungen.
7. Ergebnis dokumentieren (``sync_runs``) und Ziel aktualisieren.

Fehlerverhalten: Schreibfehler beenden den Lauf sofort (keine weiteren Änderungen, kein
automatisches Wiederholen von Schreibaufrufen). Nur Rate-Limits/Kontingent führen zu
**einem** zeitversetzten neuen Versuch; der plant anhand des tatsächlichen Stands neu und
ist dadurch idempotent. Lokale Daten werden nie gelöscht.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from musichub.connections import service as connections
from musichub.connectors.base import (
    Capability,
    ConnectorAccessError,
    ConnectorError,
    RateLimitedError,
    RemoteTrack,
    TrackQuery,
)
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings, get_settings
from musichub.db.session import get_sessionmaker
from musichub.domain.platform_urls import canonical_url
from musichub.domain.types import (
    Availability,
    LinkStatus,
    LookupStatus,
    MatchLevel,
    MatchMethod,
    Platform,
    SyncMode,
    SyncRunStatus,
)
from musichub.jobs.handlers import JobContext, JobError, handler
from musichub.matching.engine import TrackFacts, compare
from musichub.models import (
    PlatformLookup,
    Playlist,
    PlaylistItem,
    PlaylistTarget,
    SyncRun,
    Track,
    TrackSource,
)
from musichub.sync.engine import (
    DesiredItem,
    RemoteItem,
    SyncPlan,
    Unresolved,
    plan_sync,
    reorder_moves,
)
from musichub.sync.service import JOB_SYNC, write_blockers

log = logging.getLogger(__name__)

_LOOKUP_REASON = {
    LookupStatus.NOT_FOUND.value: Unresolved.NOT_FOUND,
    LookupStatus.API_ERROR.value: Unresolved.API_ERROR,
    LookupStatus.CANDIDATE.value: Unresolved.CANDIDATE,
}


def _now() -> datetime:
    return datetime.now(UTC)


def _linked_source(track: Track, platform: Platform) -> TrackSource | None:
    return next(
        (
            s
            for s in track.sources
            if s.platform == platform.value and s.link_status == LinkStatus.LINKED.value
        ),
        None,
    )


def resolve_reason(track: Track, platform: Platform, lookup: PlatformLookup | None) -> str | None:
    """None = übertragbar; sonst Grund (``Unresolved``-Wert)."""
    source = _linked_source(track, platform)
    if source is not None:
        if source.availability == Availability.UNAVAILABLE.value:
            return Unresolved.UNAVAILABLE.value
        return None
    if lookup is None:
        return Unresolved.UNCHECKED.value
    if lookup.status == LookupStatus.FOUND.value and lookup.external_id:
        return None
    return _LOOKUP_REASON.get(lookup.status, Unresolved.UNCHECKED).value


def _facts(track: Track) -> TrackFacts:
    return TrackFacts(
        title=track.title,
        artist=track.artist,
        version=track.version,
        duration_ms=track.duration_ms,
        isrc=track.isrc,
        album=track.album,
        release_date=track.release_date,
    )


def _remote_facts(t: RemoteTrack) -> TrackFacts:
    return TrackFacts(
        title=t.title,
        artist=", ".join(t.artists),
        duration_ms=t.duration_ms,
        isrc=t.isrc,
        album=t.album,
        release_date=datetime.fromisoformat(t.release_date).date() if t.release_date else None,
    )


class _Context:
    def __init__(self, ctx: JobContext, run: SyncRun, target: PlaylistTarget) -> None:
        self.ctx = ctx
        self.db = ctx.session
        self.run = run
        self.target = target
        self.platform = Platform(target.platform)
        self.settings: Settings = get_settings()
        self.registry: ConnectorRegistry = ctx.connectors
        self.connector = self.registry.get(self.platform)

    def call(self, operation: Any) -> Any:
        return connections.call_with_token(self.settings, self.registry, self.platform, operation)


# --- Schritt 2: Tracks auf der Zielplattform bestimmen -------------------------------------------


def _lookup(c: _Context, tracks: list[Track], lookups: dict[uuid.UUID, PlatformLookup]) -> int:
    """Sucht noch nicht geprüfte Tracks (und solche mit API-Fehler) auf der Plattform."""
    if Capability.SEARCH not in c.connector.capabilities:
        return 0
    budget = c.settings.sync_lookup_limit
    done = 0
    for track in tracks:
        if budget <= 0:
            break
        lookup = lookups.get(track.id)
        if lookup is not None and lookup.status != LookupStatus.API_ERROR.value:
            continue
        budget -= 1
        if lookup is None:
            lookup = PlatformLookup(track_id=track.id, platform=c.platform.value)
            c.db.add(lookup)
            lookups[track.id] = lookup
        lookup.checked_at = _now()
        query = TrackQuery(
            title=track.title, artist=track.artist, duration_ms=track.duration_ms, isrc=track.isrc
        )
        try:
            hits = c.call(lambda conn, token, q=query: conn.search(token, q, 5))
            if not hits and track.isrc:
                # ISRC unbekannt: mit Titel/Artist erneut suchen
                plain = TrackQuery(track.title, track.artist, track.duration_ms)
                hits = c.call(lambda conn, token, q=plain: conn.search(token, q, 5))
        except RateLimitedError as exc:
            lookup.status, lookup.message = LookupStatus.API_ERROR.value, exc.message
            lookup.external_id, lookup.confidence = None, None
            break  # Kontingent: weitere Suchen bleiben „noch nicht geprüft“
        except ConnectorError as exc:
            lookup.status, lookup.message = LookupStatus.API_ERROR.value, exc.message
            lookup.external_id, lookup.confidence = None, None
            if not exc.retryable:
                continue
            break
        done += 1
        best = None
        for hit in hits:
            if not hit.external_id or not hit.available:
                continue
            assessment = compare(_facts(track), _remote_facts(hit))
            if best is None or assessment.confidence > best[1].confidence:
                best = (hit, assessment)
        if best and best[1].level is MatchLevel.CERTAIN and not best[1].blockers:
            lookup.status = LookupStatus.FOUND.value
        elif best and best[1].level in (MatchLevel.LIKELY, MatchLevel.REVIEW):
            lookup.status = LookupStatus.CANDIDATE.value
        else:
            lookup.status, lookup.external_id, lookup.confidence = (
                LookupStatus.NOT_FOUND.value,
                None,
                None,
            )
            lookup.message = "Kein passender Treffer."
            continue
        lookup.external_id = best[0].external_id
        lookup.confidence = best[1].confidence
        lookup.message = f"{best[0].title} – {', '.join(best[0].artists)}"
    c.db.flush()
    return done


def _desired(
    c: _Context,
) -> tuple[list[DesiredItem], dict[uuid.UUID, Track], dict[uuid.UUID, PlatformLookup]]:
    items = c.db.scalars(
        select(PlaylistItem)
        .where(PlaylistItem.playlist_id == c.target.playlist_id)
        .order_by(PlaylistItem.position)
    ).all()
    tracks = {i.track.id: i.track for i in items}
    lookups = {
        lk.track_id: lk
        for lk in c.db.scalars(
            select(PlatformLookup).where(
                PlatformLookup.platform == c.platform.value,
                PlatformLookup.track_id.in_(list(tracks)),
            )
        )
    }
    pending = [
        t
        for t in tracks.values()
        if resolve_reason(t, c.platform, lookups.get(t.id))
        in (Unresolved.UNCHECKED.value, Unresolved.API_ERROR.value)
    ]
    if pending:
        c.ctx.progress(0, len(pending), "Fehlende Tracks werden auf der Plattform gesucht …")
        _lookup(c, pending, lookups)
    desired: list[DesiredItem] = []
    for item in items:
        track = item.track
        lookup = lookups.get(track.id)
        reason = resolve_reason(track, c.platform, lookup)
        if reason is None:
            source = _linked_source(track, c.platform)
            external = source.external_id if source else (lookup.external_id if lookup else None)
            desired.append(DesiredItem(track.id, external))
        else:
            desired.append(DesiredItem(track.id, None, Unresolved(reason)))
    return desired, tracks, lookups


# --- Schritt 3: Zielplaylist lesen ---------------------------------------------------------------


def _read_remote(c: _Context) -> tuple[list[RemoteItem], dict[str, RemoteTrack]]:
    if not c.target.remote_playlist_id:
        return [], {}
    remote_id = c.target.remote_playlist_id

    def read(conn: Any, token: str) -> list[RemoteTrack]:
        result: list[RemoteTrack] = []
        for page in conn.iter_playlist_tracks(token, remote_id):
            result.extend(page.tracks)
        return result

    tracks = c.call(read)
    items = [RemoteItem(t.external_id, t.item_id) for t in tracks if t.external_id]
    return items, {t.external_id: t for t in tracks if t.external_id}


# --- Schritt 5: Vorschau dokumentieren -----------------------------------------------------------


def _plan_json(
    c: _Context,
    plan: SyncPlan,
    tracks: dict[uuid.UUID, Track],
    lookups: dict[uuid.UUID, PlatformLookup],
    remote_titles: dict[str, RemoteTrack],
) -> dict[str, Any]:
    def track_info(track_id: uuid.UUID) -> dict[str, Any]:
        t = tracks[track_id]
        return {"track_id": str(t.id), "title": t.title, "artist": t.artist, "version": t.version}

    def remote_info(external_id: str) -> dict[str, Any]:
        t = remote_titles.get(external_id)
        return {
            "external_id": external_id,
            "title": t.title if t else None,
            "artist": ", ".join(t.artists) if t else None,
            "url": canonical_url(c.platform, external_id),
        }

    unresolved = []
    for d in plan.unresolved:
        track = tracks[d.track_id]
        lookup = lookups.get(d.track_id)
        unresolved.append(
            {
                **track_info(d.track_id),
                "reason": d.reason.value if d.reason else Unresolved.UNCHECKED.value,
                "candidate_url": canonical_url(c.platform, lookup.external_id)
                if lookup and lookup.external_id
                else None,
                "message": lookup.message if lookup else None,
                "alternatives": [
                    {
                        "platform": s.platform,
                        "url": s.url or canonical_url(Platform(s.platform), s.external_id),
                    }
                    for s in track.sources
                    if s.link_status == LinkStatus.LINKED.value and s.platform != c.platform.value
                ],
            }
        )
    newly_linked = {
        str(tid)
        for tid, lk in lookups.items()
        if lk.status == LookupStatus.FOUND.value and _linked_source(tracks[tid], c.platform) is None
    }
    moves_estimate = 0
    if plan.reorder and not c.connector.can_replace:
        moves_estimate = len(plan.target_sequence)
    writes = (
        0
        if plan.is_noop
        else (1 if c.connector.can_replace and plan.mode is SyncMode.MIRROR else 0)
        + len(plan.to_add)
        + len(plan.to_remove)
        + moves_estimate
        + (1 if not c.target.remote_playlist_id else 0)
    )
    return {
        "create_playlist": not c.target.remote_playlist_id,
        "remote_name": c.target.remote_name,
        "add": [
            {
                **remote_info(a.external_id),
                **track_info(a.track_id),
                "new_link": str(a.track_id) in newly_linked,
            }
            for a in plan.to_add
        ],
        "remove": [{**remote_info(r.external_id), "external": r.external} for r in plan.to_remove],
        "reorder": plan.reorder,
        "unresolved": unresolved,
        "duplicates_skipped": len(plan.duplicates_skipped),
        "already_present": plan.already_present,
        "external_added": [remote_info(e) for e in plan.external_added],
        "external_removed": [remote_info(e) for e in plan.external_removed],
        "not_readded": [remote_info(e) for e in plan.not_readded],
        "requires_approval": plan.requires_approval,
        "noop": plan.is_noop,
        "first_sync": c.target.known_remote_items is None,
        "quota_units": writes * c.connector.write_cost_units
        if c.connector.write_cost_units
        else None,
    }


# --- Schritt 6: Ausführen ------------------------------------------------------------------------


def _persist_remote_playlist(
    target_id: uuid.UUID, external_id: str, url: str | None, name: str
) -> None:
    """Sofort festschreiben: Schlägt der Lauf danach fehl, darf kein zweites Mal angelegt werden."""
    with get_sessionmaker().begin() as s:
        target = s.get(PlaylistTarget, target_id)
        if target is not None:
            target.remote_playlist_id = external_id
            target.remote_url = url
            target.remote_name = name


def _link_found_tracks(
    c: _Context,
    plan: SyncPlan,
    lookups: dict[uuid.UUID, PlatformLookup],
    tracks: dict[uuid.UUID, Track],
) -> int:
    """Sicher gefundene Tracks lokal verknüpfen (nur ergänzen, nie überschreiben)."""
    linked = 0
    for add in plan.to_add:
        lookup = lookups.get(add.track_id)
        track = tracks[add.track_id]
        if lookup is None or lookup.status != LookupStatus.FOUND.value:
            continue
        if _linked_source(track, c.platform) is not None:
            continue
        taken = c.db.scalar(
            select(TrackSource.id).where(
                TrackSource.platform == c.platform.value,
                TrackSource.external_id == add.external_id,
                TrackSource.link_status == LinkStatus.LINKED.value,
            )
        )
        status = LinkStatus.CANDIDATE if taken else LinkStatus.LINKED
        exists = next(
            (
                s
                for s in track.sources
                if s.platform == c.platform.value and s.external_id == add.external_id
            ),
            None,
        )
        if exists is not None:
            continue
        source = TrackSource(
            track_id=track.id,
            platform=c.platform.value,
            external_id=add.external_id,
            url=canonical_url(c.platform, add.external_id),
            link_status=status.value,
            availability=Availability.AVAILABLE.value,
            match_method=(MatchMethod.ISRC if track.isrc else MatchMethod.FUZZY).value,
            match_score=(lookup.confidence or 0) / 100,
            last_checked_at=_now(),
        )
        c.db.add(source)
        track.sources.append(source)
        linked += 1
    c.db.flush()
    return linked


def _execute(c: _Context, plan: SyncPlan, remote: list[RemoteItem]) -> None:
    target = c.target
    if not target.remote_playlist_id:
        name = target.remote_name or c.run.playlist_name
        playlist = c.db.get(Playlist, target.playlist_id)
        description = "Von MusicHub synchronisiert."
        if playlist and playlist.description:
            description = f"{playlist.description} · {description}"
        created = c.call(lambda conn, token: conn.create_remote_playlist(token, name, description))
        _persist_remote_playlist(target.id, created.external_id, created.url, created.name)
        target.remote_playlist_id = created.external_id
        target.remote_url = created.url
        target.remote_name = created.name
        c.run.remote_playlist_id = created.external_id
    playlist_id = target.remote_playlist_id
    adds = [a.external_id for a in plan.to_add]

    if plan.mode is SyncMode.MIRROR and c.connector.can_replace:
        c.call(
            lambda conn, token: conn.replace_playlist(
                token, playlist_id, list(plan.target_sequence)
            )
        )
        return
    if plan.mode is SyncMode.MIRROR:
        removals = [r.item_id for r in plan.to_remove if r.item_id]
        if len(removals) != len(plan.to_remove):
            raise ConnectorError(
                c.platform, "Einträge ohne ID können nicht entfernt werden.", retryable=False
            )
        if removals:
            c.call(lambda conn, token: conn.remove_from_playlist(token, playlist_id, removals))
    if adds:
        c.call(lambda conn, token: conn.add_to_playlist(token, playlist_id, adds))
    if plan.mode is SyncMode.MIRROR and plan.reorder:
        current, _ = _read_remote(c)
        for move in reorder_moves(current, list(plan.target_sequence)):
            ext = next(i.external_id for i in current if i.item_id == move.item_id)
            c.call(
                lambda conn, token, m=move, e=ext: conn.move_in_playlist(
                    token, playlist_id, m.item_id, e, m.position
                )
            )


def _known_after(remote_after: list[RemoteItem], desired: list[DesiredItem]) -> list[str]:
    """Von MusicHub verantwortete Einträge: Schnittmenge aus Ziel und lokaler Playlist."""
    budget = Counter(d.external_id for d in desired if d.external_id)
    known = []
    for item in remote_after:
        if budget[item.external_id] > 0:
            budget[item.external_id] -= 1
            known.append(item.external_id)
    return known


# --- Job ------------------------------------------------------------------------------------------


def _record_failure(run_id: uuid.UUID, message: str, *, status: SyncRunStatus) -> None:
    with get_sessionmaker().begin() as s:
        run = s.get(SyncRun, run_id)
        if run is None:
            return
        run.status = status.value
        run.errors = [*(run.errors or []), message[:2000]]
        if status is not SyncRunStatus.QUEUED:
            run.finished_at = _now()
        target = s.get(PlaylistTarget, run.target_id) if run.target_id else None
        if target is not None and not run.dry_run:
            target.last_status = run.status
            target.last_error = message[:2000]
            target.last_run_id = run.id


@handler(JOB_SYNC)
def run_sync(ctx: JobContext) -> None:
    try:
        run_id = uuid.UUID(str(ctx.payload["run_id"]))
    except (KeyError, ValueError):
        raise JobError("Job ohne gültigen Synchronisationslauf.", retryable=False) from None
    run = ctx.session.get(SyncRun, run_id)
    if run is None or run.status not in (SyncRunStatus.QUEUED.value, SyncRunStatus.RUNNING.value):
        return  # bereits erledigt (Job wurde wiederholt)
    try:
        _run(ctx, run)
    except Exception:
        # Job-Transaktion zuerst verwerfen (sie hält Zeilensperren), dann Fehler vermerken
        ctx.session.rollback()
        try:
            raise
        except RateLimitedError as exc:
            _record_failure(run_id, exc.message, status=SyncRunStatus.QUEUED)
            raise JobError(exc.message, retryable=True, retry_after=exc.retry_after) from None
        except ConnectorError as exc:
            _record_failure(run_id, exc.message, status=SyncRunStatus.FAILED)
            raise JobError(exc.message, retryable=False) from None
        except JobError as exc:
            _record_failure(run_id, str(exc), status=SyncRunStatus.FAILED)
            raise
        except Exception:
            log.exception("Synchronisierung abgebrochen")
            _record_failure(
                run_id, "Interner Fehler – Details im Server-Log.", status=SyncRunStatus.FAILED
            )
            raise JobError("Interner Fehler bei der Synchronisierung.", retryable=False) from None


def _run(ctx: JobContext, run: SyncRun) -> None:
    db = ctx.session
    target = db.get(PlaylistTarget, run.target_id) if run.target_id else None
    if target is None:
        raise JobError("Synchronisationsziel wurde gelöscht.", retryable=False)
    c = _Context(ctx, run, target)
    with get_sessionmaker().begin() as s:  # Status sofort sichtbar
        live = s.get(SyncRun, run.id)
        if live is not None:
            live.status = SyncRunStatus.RUNNING.value
            live.started_at = live.started_at or _now()
    run.started_at = run.started_at or _now()
    run.status = SyncRunStatus.RUNNING.value

    if not run.dry_run:
        problems = write_blockers(db, c.settings, c.registry, target)
        if target.confirmed_at is None:
            problems.append("Erste Synchronisierung wurde nicht bestätigt.")
        if run.mode != target.mode:
            problems.append("Der Modus des Ziels wurde geändert – bitte erneut starten.")
        if problems:
            _finish(c, SyncRunStatus.BLOCKED, errors=problems)
            return

    # 1.–2. Lokale Playlist und verfügbare Tracks
    ctx.progress(0, None, "Lokale Playlist wird gelesen …")
    desired, tracks, lookups = _desired(c)
    # 3. Zielplaylist
    ctx.progress(0, None, "Zielplaylist wird gelesen …")
    try:
        remote, remote_titles = _read_remote(c)
    except ConnectorAccessError as exc:
        if exc.status == 404:
            _finish(
                c,
                SyncRunStatus.FAILED,
                errors=["Zielplaylist existiert auf der Plattform nicht mehr."],
            )
            return
        raise
    # 4. Differenzen
    plan = plan_sync(c.platform, SyncMode(target.mode), desired, remote, target.known_remote_items)
    run.plan = _plan_json(c, plan, tracks, lookups, remote_titles)
    run.unmatched = len(plan.unresolved)
    run.skipped = len(plan.unresolved) + len(plan.duplicates_skipped) + len(plan.not_readded)
    run.added, run.removed = len(plan.to_add), len(plan.to_remove)

    # 5. Vorschau
    if run.dry_run:
        _finish(c, SyncRunStatus.PREVIEW)
        return

    # Sicherheitsprüfungen vor dem Schreiben
    if plan.requires_approval and not run.approved_external_removal:
        run.added = run.removed = 0
        _finish(
            c,
            SyncRunStatus.CONFLICT,
            errors=[
                "Die Zielplaylist enthält externe Änderungen. Entfernen oder Überschreiben "
                "nur mit ausdrücklicher Zustimmung in der Vorschau."
            ],
        )
        return
    if run.preview_run_id and (preview := db.get(SyncRun, run.preview_run_id)) is not None:
        seen = {r["external_id"] for r in (preview.plan or {}).get("remove", [])}
        new_removals = {r.external_id for r in plan.to_remove} - seen
        if new_removals:
            run.added = run.removed = 0
            _finish(
                c,
                SyncRunStatus.CONFLICT,
                errors=[
                    "Die Zielplaylist hat sich seit der Vorschau geändert – "
                    "bitte neue Vorschau erstellen."
                ],
            )
            return

    # 6. Ausführen
    if plan.is_noop and target.remote_playlist_id:
        _complete(c, plan, desired, remote, tracks, lookups, executed=False)
        return
    ctx.progress(0, len(plan.to_add) + len(plan.to_remove), "Änderungen werden übertragen …")
    _link_found_tracks(c, plan, lookups, tracks)
    _execute(c, plan, remote)
    remote_after, _ = _read_remote(c)
    _complete(c, plan, desired, remote_after, tracks, lookups, executed=True)


def _complete(
    c: _Context,
    plan: SyncPlan,
    desired: list[DesiredItem],
    remote_after: list[RemoteItem],
    tracks: dict[uuid.UUID, Track],
    lookups: dict[uuid.UUID, PlatformLookup],
    *,
    executed: bool,
) -> None:
    # 7. Ergebnis prüfen und dokumentieren
    after_ids = [i.external_id for i in remote_after]
    errors: list[str] = []
    status = SyncRunStatus.SUCCEEDED if executed else SyncRunStatus.NO_CHANGES
    if executed:
        missing = Counter(a.external_id for a in plan.to_add) - Counter(after_ids)
        if plan.mode is SyncMode.MIRROR and after_ids != list(plan.target_sequence):
            errors.append(
                "Die Zielplaylist entspricht nach dem Sync nicht vollständig der lokalen."
            )
            status = SyncRunStatus.PARTIAL
        elif missing:
            errors.append(
                f"{sum(missing.values())} Track(s) fehlen nach dem Sync auf der Plattform."
            )
            status = SyncRunStatus.PARTIAL
    target = c.target
    # Extern entfernte, bewusst nicht erneut hinzugefügte Tracks bleiben im Gedächtnis –
    # sonst würden sie beim übernächsten Lauf doch wieder hinzugefügt
    target.known_remote_items = _known_after(remote_after, desired) + list(plan.not_readded)
    target.last_synced_at = _now()
    if not executed:
        c.run.added = c.run.removed = 0
    _finish(c, status, errors=errors)


def _finish(c: _Context, status: SyncRunStatus, *, errors: list[str] | None = None) -> None:
    run, target = c.run, c.target
    run.status = status.value
    run.finished_at = _now()
    if errors:
        run.errors = [*(run.errors or []), *errors]
    if not run.dry_run:
        target.last_status = status.value
        target.last_run_id = run.id
        target.last_error = "; ".join(errors) if errors else None
    c.db.flush()
    c.ctx.result.update(
        run_id=str(run.id),
        status=status.value,
        added=run.added,
        removed=run.removed,
        skipped=run.skipped,
    )
    log.info(
        "Synchronisierung beendet",
        extra={"run_id": str(run.id), "status": status.value, "dry_run": run.dry_run},
    )
