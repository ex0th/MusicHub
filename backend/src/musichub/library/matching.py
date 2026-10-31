"""Matching-Dienst: Vorschläge erzeugen, entscheiden, zusammenführen.

Die Bewertung selbst ist reine Logik in ``musichub.matching``; hier liegt nur I/O.

Konservative Regeln für automatische Zusammenführungen (:func:`auto_merge`):

1. Nur Paare der Stufe ``certain`` (Confidence ≥ 95 **und** harter Beleg: gleiche ISRC
   oder exakter Titel/Artist/Version mit gleicher Dauer ±2 s).
2. Keine Blocker – z. B. zwei verschiedene Tracks derselben Plattform, einseitige oder
   abweichende Versionsangaben, nur ähnliche Artist-Namen.
3. Das Paar wird unmittelbar vor dem Zusammenführen mit den aktuellen Daten neu bewertet.
4. Abgelehnte und zurückgestellte Paare werden nie automatisch zusammengeführt.
5. Jede Zusammenführung ist protokolliert und rückgängig zu machen.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.orm import Session

from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.domain.types import (
    CandidateStatus,
    JobStatus,
    LinkStatus,
    MatchLevel,
    MergeMethod,
)
from musichub.library.merge import choose_kept, merge_tracks, ordered, reject_pair
from musichub.matching.blocking import candidate_pairs
from musichub.matching.engine import (
    ENGINE_VERSION,
    MatchAssessment,
    ParsedTrack,
    TrackFacts,
    compare,
    parse,
)
from musichub.models import MatchCandidate, PlaylistItem, Track, TrackMerge, TrackSource

log = logging.getLogger(__name__)

_LOCK_KEY = "musichub.matching"
ACTIVE = (CandidateStatus.OPEN.value, CandidateStatus.DEFERRED.value)


def _now() -> datetime:
    return datetime.now(UTC)


def _lock(db: Session) -> None:
    """Scan und automatische Zusammenführung laufen nie parallel."""
    db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": _LOCK_KEY})


# --- Fakten laden ------------------------------------------------------------------------------


def load_facts(db: Session, ids: list[uuid.UUID] | None = None) -> dict[uuid.UUID, TrackFacts]:
    stmt = select(
        Track.id,
        Track.title,
        Track.artist,
        Track.version,
        Track.duration_ms,
        Track.isrc,
        Track.album,
        Track.release_date,
    )
    src = select(TrackSource.track_id, TrackSource.platform, TrackSource.external_id).where(
        TrackSource.link_status == LinkStatus.LINKED.value
    )
    if ids is not None:
        stmt = stmt.where(Track.id.in_(ids))
        src = src.where(TrackSource.track_id.in_(ids))
    platforms: dict[uuid.UUID, dict[str, str]] = {}
    for track_id, platform, external_id in db.execute(src):
        platforms.setdefault(track_id, {})[platform] = external_id
    return {
        r.id: TrackFacts(
            title=r.title,
            artist=r.artist,
            version=r.version,
            duration_ms=r.duration_ms,
            isrc=r.isrc,
            album=r.album,
            release_date=r.release_date,
            platforms=platforms.get(r.id, {}),
        )
        for r in db.execute(stmt)
    }


def _candidate_values(result: MatchAssessment) -> dict[str, Any]:
    return {
        "confidence": result.confidence,
        "level": result.level.value,
        "reasons": [r.as_dict() for r in result.reasons],
        "blockers": list(result.blockers),
        "engine_version": ENGINE_VERSION,
    }


# --- Scan ------------------------------------------------------------------------------------


@dataclass(slots=True)
class ScanResult:
    tracks: int = 0
    unmatchable: int = 0
    compared: int = 0
    created: int = 0
    updated: int = 0
    removed: int = 0
    levels: dict[str, int] = field(default_factory=dict)


def scan(db: Session) -> ScanResult:
    """Vergleicht alle Tracks und aktualisiert die Vorschläge. Der Aufrufer committet.

    - neue Paare ab Stufe ``review`` werden als ``open`` angelegt,
    - offene/zurückgestellte Paare werden neu bewertet (Status bleibt) oder entfernt,
      wenn sie nicht mehr ähnlich genug sind,
    - abgelehnte Paare bleiben unverändert und werden nicht erneut vorgeschlagen.
    """
    _lock(db)
    facts = load_facts(db)
    parsed: dict[uuid.UUID, ParsedTrack] = {k: parse(v) for k, v in facts.items()}
    result = ScanResult(tracks=len(parsed))
    result.unmatchable = sum(1 for p in parsed.values() if not p.matchable)

    existing = {(c.track_a_id, c.track_b_id): c for c in db.scalars(select(MatchCandidate)).all()}
    pairs = {ordered(a, b) for a, b in candidate_pairs(parsed.items())}
    pairs |= {pair for pair, c in existing.items() if c.status in ACTIVE}

    new_rows: list[dict[str, Any]] = []
    stale: list[uuid.UUID] = []
    for a, b in pairs:
        row = existing.get((a, b))
        if row is not None and row.status == CandidateStatus.REJECTED.value:
            continue
        assessment = compare(parsed[a], parsed[b])
        result.compared += 1
        if assessment.level is MatchLevel.DIFFERENT:
            if row is not None:
                stale.append(row.id)
            continue
        result.levels[assessment.level.value] = result.levels.get(assessment.level.value, 0) + 1
        values = _candidate_values(assessment)
        if row is None:
            new_rows.append(
                {
                    "id": uuid.uuid4(),
                    "track_a_id": a,
                    "track_b_id": b,
                    "status": CandidateStatus.OPEN.value,
                    **values,
                }
            )
        elif any(getattr(row, k) != v for k, v in values.items()):
            for key, value in values.items():
                setattr(row, key, value)
            result.updated += 1
    if new_rows:
        db.execute(insert(MatchCandidate), new_rows)
    if stale:
        db.execute(delete(MatchCandidate).where(MatchCandidate.id.in_(stale)))
    result.created, result.removed = len(new_rows), len(stale)
    db.flush()
    log.info(
        "Matching-Scan abgeschlossen",
        extra={
            "tracks": result.tracks,
            "compared": result.compared,
            "new_candidates": result.created,
        },
    )
    return result


def request_scan(db: Session) -> bool:
    """Scan als Hintergrundjob einplanen, falls nicht schon einer wartet."""
    from musichub.jobs.queue import enqueue
    from musichub.models import Job

    waiting = db.scalar(
        select(func.count()).where(
            Job.kind == "matching.scan", Job.status == JobStatus.QUEUED.value
        )
    )
    if waiting:
        return False
    enqueue(db, "matching.scan", max_attempts=3)
    return True


# --- Lesen ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateView:
    candidate: MatchCandidate
    #: neuerer Track („Quelltrack“)
    source: Track
    #: älterer, etablierter Track („Vorgeschlagener Track“)
    suggested: Track
    playlist_counts: dict[uuid.UUID, int]


def playlist_counts(db: Session, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    if not ids:
        return {}
    rows = db.execute(
        select(PlaylistItem.track_id, func.count(func.distinct(PlaylistItem.playlist_id)))
        .where(PlaylistItem.track_id.in_(ids))
        .group_by(PlaylistItem.track_id)
    )
    return {track_id: count for track_id, count in rows}


def _view(db: Session, cand: MatchCandidate, tracks: dict[uuid.UUID, Track]) -> CandidateView:
    a, b = tracks[cand.track_a_id], tracks[cand.track_b_id]
    source, suggested = (a, b) if (a.created_at, str(a.id)) > (b.created_at, str(b.id)) else (b, a)
    return CandidateView(cand, source, suggested, {})


def list_candidates(
    db: Session,
    *,
    statuses: list[CandidateStatus],
    levels: list[MatchLevel] | None,
    limit: int,
    offset: int,
) -> tuple[list[CandidateView], int]:
    stmt = select(MatchCandidate).where(MatchCandidate.status.in_([s.value for s in statuses]))
    if levels:
        stmt = stmt.where(MatchCandidate.level.in_([lv.value for lv in levels]))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = db.scalars(
        stmt.order_by(
            MatchCandidate.confidence.desc(), MatchCandidate.updated_at.desc(), MatchCandidate.id
        )
        .limit(limit)
        .offset(offset)
    ).all()
    ids = list({i for c in rows for i in (c.track_a_id, c.track_b_id)})
    tracks = {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(ids)))}
    counts = playlist_counts(db, ids)
    views = []
    for cand in rows:
        view = _view(db, cand, tracks)
        views.append(
            CandidateView(
                cand,
                view.source,
                view.suggested,
                {k: counts.get(k, 0) for k in (cand.track_a_id, cand.track_b_id)},
            )
        )
    return views, total


def get_candidate(db: Session, candidate_id: uuid.UUID, *, lock: bool = False) -> MatchCandidate:
    stmt = select(MatchCandidate).where(MatchCandidate.id == candidate_id)
    if lock:
        stmt = stmt.with_for_update()
    cand = db.scalar(stmt)
    if cand is None:
        raise NotFoundError(f"Vorschlag {candidate_id} nicht gefunden.")
    return cand


def candidate_view(db: Session, cand: MatchCandidate) -> CandidateView:
    ids = [cand.track_a_id, cand.track_b_id]
    tracks = {t.id: t for t in db.scalars(select(Track).where(Track.id.in_(ids)))}
    view = _view(db, cand, tracks)
    counts = playlist_counts(db, ids)
    return CandidateView(cand, view.source, view.suggested, {k: counts.get(k, 0) for k in ids})


def summary(db: Session) -> dict[str, Any]:
    rows = db.execute(
        select(MatchCandidate.status, MatchCandidate.level, func.count()).group_by(
            MatchCandidate.status, MatchCandidate.level
        )
    ).all()
    by_status: dict[str, int] = {s.value: 0 for s in CandidateStatus}
    open_levels: dict[str, int] = {
        lv.value: 0 for lv in MatchLevel if lv is not MatchLevel.DIFFERENT
    }
    for status, level, count in rows:
        by_status[status] = by_status.get(status, 0) + count
        if status == CandidateStatus.OPEN.value and level in open_levels:
            open_levels[level] += count
    auto_ready = db.scalar(
        select(func.count()).where(
            MatchCandidate.status == CandidateStatus.OPEN.value,
            MatchCandidate.level == MatchLevel.CERTAIN.value,
            func.jsonb_array_length(MatchCandidate.blockers) == 0,
        )
    )
    merges = db.scalar(select(func.count()).select_from(TrackMerge)) or 0
    last_scan = db.scalar(select(func.max(MatchCandidate.updated_at)))
    return {
        "statuses": by_status,
        "open_levels": open_levels,
        "auto_merge_ready": auto_ready or 0,
        "merges": merges,
        "last_change": last_scan,
    }


# --- Entscheidungen ----------------------------------------------------------------------------


def _decide(cand: MatchCandidate, status: CandidateStatus, by: str) -> MatchCandidate:
    cand.status = status.value
    cand.decided_at, cand.decided_by = _now(), by
    return cand


def reject(db: Session, candidate_id: uuid.UUID, *, by: str) -> MatchCandidate:
    cand = get_candidate(db, candidate_id, lock=True)
    _decide(cand, CandidateStatus.REJECTED, by)
    db.flush()
    return cand


def defer(db: Session, candidate_id: uuid.UUID, *, by: str) -> MatchCandidate:
    cand = get_candidate(db, candidate_id, lock=True)
    if cand.status == CandidateStatus.REJECTED.value:
        raise ConflictError("Abgelehnte Vorschläge zuerst wieder öffnen.")
    _decide(cand, CandidateStatus.DEFERRED, by)
    db.flush()
    return cand


def reopen(db: Session, candidate_id: uuid.UUID, *, by: str) -> MatchCandidate:
    """Korrigiert eine Ablehnung oder holt einen zurückgestellten Vorschlag zurück."""
    cand = get_candidate(db, candidate_id, lock=True)
    facts = load_facts(db, [cand.track_a_id, cand.track_b_id])
    assessment = compare(facts[cand.track_a_id], facts[cand.track_b_id])
    for key, value in _candidate_values(assessment).items():
        setattr(cand, key, value)
    _decide(cand, CandidateStatus.OPEN, by)
    db.flush()
    return cand


def confirm(db: Session, candidate_id: uuid.UUID, *, keep: str, by: str) -> TrackMerge:
    """Bestätigt einen Vorschlag: führt den Quelltrack in den vorgeschlagenen zusammen
    (``keep="suggested"``) oder umgekehrt (``keep="source"``)."""
    cand = get_candidate(db, candidate_id, lock=True)
    if cand.status == CandidateStatus.REJECTED.value:
        raise ConflictError("Der Vorschlag wurde abgelehnt. Zuerst wieder öffnen.")
    view = candidate_view(db, cand)
    keep_track, remove_track = (
        (view.suggested, view.source) if keep == "suggested" else (view.source, view.suggested)
    )
    facts = load_facts(db, [keep_track.id, remove_track.id])
    assessment = compare(facts[keep_track.id], facts[remove_track.id])
    return merge_tracks(
        db,
        keep_id=keep_track.id,
        remove_id=remove_track.id,
        by=by,
        method=MergeMethod.MANUAL,
        confidence=assessment.confidence,
        level=assessment.level,
    )


def compare_tracks(
    db: Session, a: uuid.UUID, b: uuid.UUID
) -> tuple[MatchAssessment, MatchCandidate | None]:
    if a == b:
        raise UnprocessableError("Bitte zwei verschiedene Tracks wählen.")
    facts = load_facts(db, [a, b])
    for track_id in (a, b):
        if track_id not in facts:
            raise NotFoundError(f"Track {track_id} nicht gefunden.")
    first, second = ordered(a, b)
    cand = db.scalar(
        select(MatchCandidate).where(
            MatchCandidate.track_a_id == first, MatchCandidate.track_b_id == second
        )
    )
    return compare(facts[a], facts[b]), cand


def manual_merge(db: Session, *, keep_id: uuid.UUID, remove_id: uuid.UUID, by: str) -> TrackMerge:
    """Zwei beliebige Tracks zusammenführen – auch ohne Vorschlag, auch bei Blockern.

    Abgelehnte Paare werden nur nach erneutem Öffnen zusammengeführt (Schutz vor
    versehentlichem Zusammenführen bekannter Unterschiede).
    """
    assessment, cand = compare_tracks(db, keep_id, remove_id)
    if cand is not None and cand.status == CandidateStatus.REJECTED.value:
        raise ConflictError(
            "Dieses Paar wurde als unterschiedlich markiert. Zuerst den Vorschlag wieder öffnen.",
            extra={"candidate_id": str(cand.id)},
        )
    return merge_tracks(
        db,
        keep_id=keep_id,
        remove_id=remove_id,
        by=by,
        method=MergeMethod.MANUAL,
        confidence=assessment.confidence,
        level=assessment.level,
    )


def reject_tracks(db: Session, a: uuid.UUID, b: uuid.UUID, *, by: str) -> MatchCandidate:
    assessment, _ = compare_tracks(db, a, b)
    return reject_pair(
        db, a, b, by=by, confidence=assessment.confidence, level=assessment.level.value
    )


# --- Automatisch -------------------------------------------------------------------------------


@dataclass(slots=True)
class AutoMergeResult:
    merged: list[TrackMerge] = field(default_factory=list)
    skipped: int = 0


def auto_merge(db: Session, *, by: str, limit: int = 500) -> AutoMergeResult:
    """Führt ausschließlich sichere, blockerfreie offene Vorschläge zusammen."""
    _lock(db)
    result = AutoMergeResult()
    candidates = db.scalars(
        select(MatchCandidate)
        .where(
            MatchCandidate.status == CandidateStatus.OPEN.value,
            MatchCandidate.level == MatchLevel.CERTAIN.value,
        )
        .order_by(MatchCandidate.confidence.desc(), MatchCandidate.created_at)
        .limit(limit)
    ).all()
    for cand_id, a, b in [(c.id, c.track_a_id, c.track_b_id) for c in candidates]:
        # Ein früherer Merge kann das Paar bereits aufgelöst haben (CASCADE)
        if db.get(MatchCandidate, cand_id) is None:
            continue
        facts = load_facts(db, [a, b])
        if a not in facts or b not in facts:
            continue
        assessment = compare(facts[a], facts[b])
        if not assessment.auto_merge_allowed:
            # Daten haben sich geändert: Vorschlag aktualisieren, nicht zusammenführen
            cand = db.get(MatchCandidate, cand_id)
            assert cand is not None
            for key, value in _candidate_values(assessment).items():
                setattr(cand, key, value)
            result.skipped += 1
            continue
        keep_id, remove_id = choose_kept(db, a, b)
        result.merged.append(
            merge_tracks(
                db,
                keep_id=keep_id,
                remove_id=remove_id,
                by=by,
                method=MergeMethod.AUTO,
                confidence=assessment.confidence,
                level=assessment.level,
            )
        )
    db.flush()
    return result


def list_merges(db: Session, *, limit: int, offset: int) -> tuple[list[TrackMerge], int]:
    total = db.scalar(select(func.count()).select_from(TrackMerge)) or 0
    rows = db.scalars(
        select(TrackMerge)
        .order_by(TrackMerge.created_at.desc(), TrackMerge.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return list(rows), total
