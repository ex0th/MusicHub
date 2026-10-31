"""Matching: Vorschläge für doppelte Tracks prüfen, zusammenführen, korrigieren."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from musichub.api.schemas import (
    AutoMergeOut,
    MatchCandidateOut,
    MatchCompareOut,
    MatchCompareRequest,
    MatchConfirm,
    MatchReasonOut,
    MatchScanOut,
    MatchSummaryOut,
    MatchTrackOut,
    MergeOut,
    MergeRequest,
    Page,
    TrackOut,
)
from musichub.auth import Principal, current_principal, require_write
from musichub.db.session import get_db
from musichub.domain.types import CandidateStatus, MatchLevel
from musichub.library import matching as service
from musichub.library.merge import undo_merge
from musichub.matching.engine import version_of
from musichub.models import MatchCandidate, Track, TrackMerge

router = APIRouter(prefix="/api/v1/matching", tags=["matching"])


def _track(track: Track, playlist_count: int) -> MatchTrackOut:
    data = TrackOut.model_validate(track).model_dump(exclude={"platforms"})
    return MatchTrackOut(
        **data,
        playlist_count=playlist_count,
        detected_version=version_of(track.title, track.version).describe(),
    )


def _candidate(view: service.CandidateView) -> MatchCandidateOut:
    c = view.candidate
    return MatchCandidateOut(
        id=c.id,
        status=CandidateStatus(c.status),
        confidence=c.confidence,
        level=MatchLevel(c.level),
        reasons=[MatchReasonOut.model_validate(r) for r in c.reasons],
        blockers=list(c.blockers),
        auto_merge_allowed=c.level == MatchLevel.CERTAIN.value and not c.blockers,
        engine_version=c.engine_version,
        decided_at=c.decided_at,
        decided_by=c.decided_by,
        created_at=c.created_at,
        updated_at=c.updated_at,
        source=_track(view.source, view.playlist_counts.get(view.source.id, 0)),
        suggested=_track(view.suggested, view.playlist_counts.get(view.suggested.id, 0)),
    )


def _candidate_out(db: Session, cand: MatchCandidate) -> MatchCandidateOut:
    return _candidate(service.candidate_view(db, cand))


def merge_out(merge: TrackMerge) -> MergeOut:
    snap = merge.snapshot
    changes = {
        "sources": len(snap.get("sources", [])),
        "sources_demoted": sum(
            1 for s in snap.get("sources", []) if s.get("action") == "moved_demoted"
        ),
        "playlist_items": len(snap.get("playlist_items", [])),
        "tags_added": len(snap.get("tags_added_to_kept", [])),
        "import_items": len(snap.get("import_match_items", []))
        + len(snap.get("import_result_items", [])),
    }
    return MergeOut.model_validate(
        {
            **{k: getattr(merge, k) for k in MergeOut.model_fields if hasattr(merge, k)},
            "changes": changes,
            "filled_fields": sorted(snap.get("filled", {})),
        }
    )


def _who(principal: Principal) -> str:
    return principal.display_name or principal.subject


@router.post("/scan", response_model=MatchScanOut, summary="Vorschläge erzeugen bzw. aktualisieren")
def scan(db: Session = Depends(get_db), _: Principal = Depends(require_write)) -> MatchScanOut:
    result = service.scan(db)
    db.commit()
    return MatchScanOut.model_validate(result)


@router.get("/summary", response_model=MatchSummaryOut, summary="Anzahl Vorschläge je Status")
def summary(
    db: Session = Depends(get_db), _: Principal = Depends(current_principal)
) -> MatchSummaryOut:
    return MatchSummaryOut.model_validate(service.summary(db))


@router.get(
    "/candidates", response_model=Page[MatchCandidateOut], summary="Vorschläge, beste zuerst"
)
def list_candidates(
    status: list[CandidateStatus] = Query([CandidateStatus.OPEN]),
    level: list[MatchLevel] = Query([]),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[MatchCandidateOut]:
    views, total = service.list_candidates(
        db, statuses=status, levels=level or None, limit=limit, offset=offset
    )
    return Page[MatchCandidateOut](
        items=[_candidate(v) for v in views], total=total, limit=limit, offset=offset
    )


@router.get("/candidates/{candidate_id}", response_model=MatchCandidateOut, summary="Vorschlag")
def get_candidate(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> MatchCandidateOut:
    return _candidate_out(db, service.get_candidate(db, candidate_id))


@router.post(
    "/candidates/{candidate_id}/confirm",
    response_model=MergeOut,
    summary="Bestätigen: beide Tracks zusammenführen",
)
def confirm(
    candidate_id: uuid.UUID,
    body: MatchConfirm,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MergeOut:
    merge = service.confirm(db, candidate_id, keep=body.keep, by=_who(principal))
    db.commit()
    return merge_out(merge)


@router.post(
    "/candidates/{candidate_id}/reject",
    response_model=MatchCandidateOut,
    summary="Ablehnen: unterschiedliche Tracks, nicht erneut vorschlagen",
)
def reject(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MatchCandidateOut:
    cand = service.reject(db, candidate_id, by=_who(principal))
    db.commit()
    return _candidate_out(db, cand)


@router.post(
    "/candidates/{candidate_id}/defer",
    response_model=MatchCandidateOut,
    summary="Später prüfen",
)
def defer(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MatchCandidateOut:
    cand = service.defer(db, candidate_id, by=_who(principal))
    db.commit()
    return _candidate_out(db, cand)


@router.post(
    "/candidates/{candidate_id}/reopen",
    response_model=MatchCandidateOut,
    summary="Ablehnung oder Zurückstellung aufheben (neu bewertet)",
)
def reopen(
    candidate_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MatchCandidateOut:
    cand = service.reopen(db, candidate_id, by=_who(principal))
    db.commit()
    return _candidate_out(db, cand)


@router.get("/compare", response_model=MatchCompareOut, summary="Zwei Tracks vergleichen")
def compare(
    track_a: uuid.UUID = Query(...),
    track_b: uuid.UUID = Query(...),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> MatchCompareOut:
    assessment, cand = service.compare_tracks(db, track_a, track_b)
    a, b = db.get(Track, track_a), db.get(Track, track_b)
    assert a is not None and b is not None
    counts = service.playlist_counts(db, [track_a, track_b])
    return MatchCompareOut(
        confidence=assessment.confidence,
        level=assessment.level,
        reasons=[MatchReasonOut.model_validate(r.as_dict()) for r in assessment.reasons],
        blockers=list(assessment.blockers),
        auto_merge_allowed=assessment.auto_merge_allowed,
        version_a=assessment.version_a,
        version_b=assessment.version_b,
        duration_diff_ms=assessment.duration_diff_ms,
        isrc_match=assessment.isrc_match,
        candidate_id=cand.id if cand else None,
        candidate_status=CandidateStatus(cand.status) if cand else None,
        track_a=_track(a, counts.get(track_a, 0)),
        track_b=_track(b, counts.get(track_b, 0)),
    )


@router.post(
    "/reject-pair",
    response_model=MatchCandidateOut,
    summary="Zwei Tracks als unterschiedlich markieren",
)
def reject_pair(
    body: MatchCompareRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MatchCandidateOut:
    cand = service.reject_tracks(db, body.track_a_id, body.track_b_id, by=_who(principal))
    db.commit()
    return _candidate_out(db, cand)


@router.post("/merge", response_model=MergeOut, summary="Zwei Tracks manuell zusammenführen")
def merge(
    body: MergeRequest,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MergeOut:
    result = service.manual_merge(
        db, keep_id=body.keep_track_id, remove_id=body.merge_track_id, by=_who(principal)
    )
    db.commit()
    return merge_out(result)


@router.post(
    "/auto-merge",
    response_model=AutoMergeOut,
    summary="Nur sichere, blockerfreie Vorschläge automatisch zusammenführen",
)
def auto_merge(
    db: Session = Depends(get_db), principal: Principal = Depends(require_write)
) -> AutoMergeOut:
    result = service.auto_merge(db, by=_who(principal))
    db.commit()
    return AutoMergeOut(merged=[merge_out(m) for m in result.merged], skipped=result.skipped)


@router.get("/merges", response_model=Page[MergeOut], summary="Verlauf der Zusammenführungen")
def list_merges(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: Principal = Depends(current_principal),
) -> Page[MergeOut]:
    rows, total = service.list_merges(db, limit=limit, offset=offset)
    return Page[MergeOut](
        items=[merge_out(m) for m in rows], total=total, limit=limit, offset=offset
    )


@router.post(
    "/merges/{merge_id}/undo", response_model=MergeOut, summary="Zusammenführung rückgängig"
)
def undo(
    merge_id: uuid.UUID,
    db: Session = Depends(get_db),
    principal: Principal = Depends(require_write),
) -> MergeOut:
    merge_row = undo_merge(db, merge_id, by=_who(principal))
    db.commit()
    return merge_out(merge_row)
