"""Zwei lokale Tracks zusammenführen – nachvollziehbar und rückgängig zu machen.

Regeln (lokal führt, nichts geht verloren):

- Titel und Artist des behaltenen Tracks werden **nie** geändert. Andere Felder werden
  nur ergänzt, wenn sie beim behaltenen Track leer sind.
- Plattform-Quellen wandern zum behaltenen Track. Hat er auf derselben Plattform schon eine
  bestätigte Quelle, wird die übernommene zum Vorschlag (``candidate``) herabgestuft statt
  sie zu verwerfen. Exakt gleiche Quellen werden zusammengelegt.
- Tags werden vereinigt, Playlist-Einträge zeigen auf den behaltenen Track – **Positionen
  bleiben unverändert**, auch doppelte Einträge bleiben erhalten.
- Verweise im Importprotokoll werden umgehängt.
- Abgelehnte Paare des entfernten Tracks gelten danach auch für den behaltenen Track.

Alles, was sich ändert, steht im ``snapshot`` des :class:`TrackMerge`-Protokolls.
:func:`undo_merge` stellt daraus den entfernten Track mit derselben ID wieder her.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.orm import Session

from musichub.core.errors import ConflictError, NotFoundError, UnprocessableError
from musichub.domain.types import (
    CandidateStatus,
    LinkStatus,
    MatchLevel,
    MergeMethod,
    MergeStatus,
)
from musichub.models import (
    ImportItem,
    MatchCandidate,
    PlaylistItem,
    Tag,
    Track,
    TrackMerge,
    TrackSource,
    track_tags,
)

log = logging.getLogger(__name__)

#: Felder, die beim behaltenen Track ergänzt werden dürfen (nie überschrieben)
FILLABLE = ("album", "version", "duration_ms", "isrc", "release_date", "notes")
_TRACK_FIELDS = ("title", "artist", *FILLABLE)
_SOURCE_FIELDS = (
    "id",
    "platform",
    "external_id",
    "url",
    "link_status",
    "availability",
    "match_method",
    "match_score",
    "last_checked_at",
    "created_at",
)
_CANDIDATE_FIELDS = (
    "id",
    "track_a_id",
    "track_b_id",
    "confidence",
    "level",
    "reasons",
    "blockers",
    "engine_version",
    "status",
    "decided_at",
    "decided_by",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _json(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def _row(obj: object, fields: tuple[str, ...]) -> dict[str, Any]:
    return {f: _json(getattr(obj, f)) for f in fields}


def _parse_field(name: str, value: Any) -> Any:
    if value is None:
        return None
    if name == "release_date":
        return date.fromisoformat(value)
    if name in {"last_checked_at", "created_at", "updated_at", "decided_at"}:
        return datetime.fromisoformat(value)
    if name in {"id", "track_id", "track_a_id", "track_b_id"}:
        return uuid.UUID(value)
    return value


def ordered(a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (a, b) if str(a) < str(b) else (b, a)


def _lock_tracks(db: Session, *ids: uuid.UUID) -> dict[uuid.UUID, Track]:
    rows = db.scalars(
        select(Track).where(Track.id.in_(ids)).order_by(Track.id).with_for_update(of=Track)
    ).all()
    return {t.id: t for t in rows}


def choose_kept(db: Session, a: uuid.UUID, b: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    """Für automatische Zusammenführungen: mehr Quellen, dann mehr Playlists, dann älter."""

    def weight(track_id: uuid.UUID) -> tuple[int, int, float]:
        sources = db.scalar(
            select(func.count()).where(
                TrackSource.track_id == track_id,
                TrackSource.link_status == LinkStatus.LINKED.value,
            )
        )
        items = db.scalar(select(func.count()).where(PlaylistItem.track_id == track_id))
        created = db.scalar(select(Track.created_at).where(Track.id == track_id))
        assert created is not None
        # ältere Tracks gewinnen: negativer Zeitstempel
        return (sources or 0, items or 0, -created.timestamp())

    return (a, b) if weight(a) >= weight(b) else (b, a)


def merge_tracks(
    db: Session,
    *,
    keep_id: uuid.UUID,
    remove_id: uuid.UUID,
    by: str,
    method: MergeMethod = MergeMethod.MANUAL,
    confidence: int | None = None,
    level: MatchLevel | None = None,
) -> TrackMerge:
    """Führt ``remove_id`` in ``keep_id`` zusammen. Der Aufrufer committet."""
    if keep_id == remove_id:
        raise UnprocessableError("Ein Track kann nicht mit sich selbst zusammengeführt werden.")
    tracks = _lock_tracks(db, keep_id, remove_id)
    for track_id in (keep_id, remove_id):
        if track_id not in tracks:
            raise NotFoundError(f"Track {track_id} nicht gefunden.")
    kept, removed = tracks[keep_id], tracks[remove_id]

    snapshot: dict[str, Any] = {
        "removed_track": {
            **_row(removed, _TRACK_FIELDS),
            "id": str(removed.id),
            "created_at": _json(removed.created_at),
        }
    }

    # Tags vereinigen
    removed_tags = set(
        db.scalars(select(track_tags.c.tag_id).where(track_tags.c.track_id == remove_id))
    )
    kept_tags = set(db.scalars(select(track_tags.c.tag_id).where(track_tags.c.track_id == keep_id)))
    added_tags = sorted(removed_tags - kept_tags, key=str)
    if added_tags:
        db.execute(insert(track_tags), [{"track_id": keep_id, "tag_id": t} for t in added_tags])
    snapshot["tags_removed"] = [str(t) for t in sorted(removed_tags, key=str)]
    snapshot["tags_added_to_kept"] = [str(t) for t in added_tags]

    # Quellen übernehmen
    kept_sources = list(db.scalars(select(TrackSource).where(TrackSource.track_id == keep_id)))
    kept_linked = {s.platform for s in kept_sources if s.link_status == LinkStatus.LINKED.value}
    kept_by_key = {(s.platform, s.external_id): s for s in kept_sources}
    moved: list[dict[str, Any]] = []
    kept_changes: list[dict[str, Any]] = []
    for source in db.scalars(select(TrackSource).where(TrackSource.track_id == remove_id)):
        entry = _row(source, _SOURCE_FIELDS)
        twin = kept_by_key.get((source.platform, source.external_id))
        if twin is not None:
            entry["action"] = "deleted_duplicate"
            if (
                source.link_status == LinkStatus.LINKED.value
                and twin.link_status != LinkStatus.LINKED.value
                and source.platform not in kept_linked
            ):
                kept_changes.append({"id": str(twin.id), "link_status": twin.link_status})
                db.execute(delete(TrackSource).where(TrackSource.id == source.id))
                db.flush()
                twin.link_status = LinkStatus.LINKED.value
                kept_linked.add(source.platform)
            else:
                db.execute(delete(TrackSource).where(TrackSource.id == source.id))
        else:
            values: dict[str, Any] = {"track_id": keep_id}
            entry["action"] = "moved"
            if source.link_status == LinkStatus.LINKED.value:
                if source.platform in kept_linked:
                    values["link_status"] = LinkStatus.CANDIDATE.value
                    entry["action"] = "moved_demoted"
                else:
                    kept_linked.add(source.platform)
            db.execute(update(TrackSource).where(TrackSource.id == source.id).values(**values))
        moved.append(entry)
    snapshot["sources"] = moved
    snapshot["kept_source_changes"] = kept_changes

    # Playlist-Einträge und Importprotokoll umhängen (Positionen bleiben unverändert)
    item_ids = list(
        db.scalars(
            update(PlaylistItem)
            .where(PlaylistItem.track_id == remove_id)
            .values(track_id=keep_id)
            .returning(PlaylistItem.id)
        )
    )
    snapshot["playlist_items"] = [str(i) for i in item_ids]
    for column, key in (
        (ImportItem.match_track_id, "import_match_items"),
        (ImportItem.result_track_id, "import_result_items"),
    ):
        ids = list(
            db.scalars(
                update(ImportItem)
                .where(column == remove_id)
                .values({column.key: keep_id})
                .returning(ImportItem.id)
            )
        )
        snapshot[key] = [str(i) for i in ids]

    # Kandidaten des entfernten Tracks sichern; Ablehnungen auf den behaltenen übertragen
    candidates = list(
        db.scalars(
            select(MatchCandidate).where(
                (MatchCandidate.track_a_id == remove_id) | (MatchCandidate.track_b_id == remove_id)
            )
        )
    )
    snapshot["candidates_removed"] = [_row(c, _CANDIDATE_FIELDS) for c in candidates]
    inserted: list[str] = []
    updated: list[dict[str, Any]] = []
    for cand in candidates:
        if cand.status != CandidateStatus.REJECTED.value:
            continue
        other = cand.track_b_id if cand.track_a_id == remove_id else cand.track_a_id
        if other == keep_id:
            continue
        a, b = ordered(keep_id, other)
        existing = db.scalar(
            select(MatchCandidate).where(
                MatchCandidate.track_a_id == a, MatchCandidate.track_b_id == b
            )
        )
        if existing is None:
            new = MatchCandidate(
                track_a_id=a,
                track_b_id=b,
                confidence=cand.confidence,
                level=cand.level,
                reasons=cand.reasons,
                blockers=cand.blockers,
                engine_version=cand.engine_version,
                status=CandidateStatus.REJECTED.value,
                decided_at=cand.decided_at,
                decided_by=cand.decided_by,
            )
            db.add(new)
            db.flush()
            inserted.append(str(new.id))
        elif existing.status != CandidateStatus.REJECTED.value:
            updated.append(_row(existing, ("id", "status", "decided_at", "decided_by")))
            existing.status = CandidateStatus.REJECTED.value
            existing.decided_at, existing.decided_by = cand.decided_at, cand.decided_by
    snapshot["candidates_transferred"] = {"inserted": inserted, "updated": updated}

    # Entfernten Track löschen (Kandidaten und Tag-Zuordnungen per CASCADE)
    removed_data = {f: getattr(removed, f) for f in FILLABLE}
    db.expunge(removed)
    db.execute(delete(Track).where(Track.id == remove_id))

    # Leere Felder des behaltenen Tracks ergänzen
    filled: dict[str, Any] = {}
    for name in FILLABLE:
        if getattr(kept, name) in (None, "") and removed_data[name] not in (None, ""):
            setattr(kept, name, removed_data[name])
            filled[name] = _json(removed_data[name])
    snapshot["filled"] = filled
    kept.updated_at = _now()

    merge = TrackMerge(
        kept_track_id=keep_id,
        removed_track_id=remove_id,
        kept_title=kept.title,
        kept_artist=kept.artist,
        removed_title=snapshot["removed_track"]["title"],
        removed_artist=snapshot["removed_track"]["artist"],
        method=method.value,
        confidence=confidence,
        level=level.value if level else None,
        status=MergeStatus.MERGED.value,
        snapshot=snapshot,
        created_by=by,
    )
    db.add(merge)
    db.flush()
    db.expire_all()
    log.info(
        "Tracks zusammengeführt",
        extra={"kept": str(keep_id), "removed": str(remove_id), "method": method.value},
    )
    return merge


def undo_merge(db: Session, merge_id: uuid.UUID, *, by: str) -> TrackMerge:
    """Stellt den entfernten Track mit derselben ID wieder her. Der Aufrufer committet.

    Nach dem Rückgängigmachen wird das Paar als „abgelehnt“ gespeichert, damit es nicht
    sofort wieder vorgeschlagen oder automatisch zusammengeführt wird.
    """
    merge = db.scalar(select(TrackMerge).where(TrackMerge.id == merge_id).with_for_update())
    if merge is None:
        raise NotFoundError(f"Zusammenführung {merge_id} nicht gefunden.")
    if merge.status != MergeStatus.MERGED.value:
        raise ConflictError("Diese Zusammenführung wurde bereits rückgängig gemacht.")
    kept = _lock_tracks(db, merge.kept_track_id).get(merge.kept_track_id)
    if kept is None:
        raise ConflictError(
            "Der behaltene Track existiert nicht mehr. Falls er selbst zusammengeführt wurde, "
            "zuerst diese spätere Zusammenführung rückgängig machen.",
            extra={"track_id": str(merge.kept_track_id)},
        )
    if db.get(Track, merge.removed_track_id) is not None:
        raise ConflictError("Der entfernte Track existiert bereits wieder.")

    snap = merge.snapshot
    keep_id, remove_id = merge.kept_track_id, merge.removed_track_id

    # Ergänzte Felder zurücknehmen, sofern sie seitdem nicht geändert wurden
    for name, value in snap.get("filled", {}).items():
        if _json(getattr(kept, name)) == value:
            setattr(kept, name, None)
    kept.updated_at = _now()

    data = snap["removed_track"]
    db.execute(
        insert(Track).values(
            id=remove_id,
            created_at=_parse_field("created_at", data["created_at"]),
            updated_at=_now(),
            **{f: _parse_field(f, data[f]) for f in _TRACK_FIELDS},
        )
    )

    # Tags
    added = [uuid.UUID(t) for t in snap.get("tags_added_to_kept", [])]
    if added:
        db.execute(
            delete(track_tags).where(
                track_tags.c.track_id == keep_id, track_tags.c.tag_id.in_(added)
            )
        )
    restore_tags = [uuid.UUID(t) for t in snap.get("tags_removed", [])]
    if restore_tags:
        alive = set(db.scalars(select(Tag.id).where(Tag.id.in_(restore_tags))))
        rows = [{"track_id": remove_id, "tag_id": t} for t in restore_tags if t in alive]
        if rows:
            db.execute(insert(track_tags), rows)

    # Herabgestufte/zusammengelegte Quellen des behaltenen Tracks zurücksetzen
    for change in snap.get("kept_source_changes", []):
        db.execute(
            update(TrackSource)
            .where(TrackSource.id == uuid.UUID(change["id"]), TrackSource.track_id == keep_id)
            .values(link_status=change["link_status"])
        )
    db.flush()

    # Quellen zurückverschieben bzw. wiederherstellen
    for entry in snap.get("sources", []):
        source_id = uuid.UUID(entry["id"])
        current = db.get(TrackSource, source_id)
        status = entry["link_status"]
        if status == LinkStatus.LINKED.value and _external_linked_elsewhere(
            db, entry["platform"], entry["external_id"], source_id
        ):
            status = LinkStatus.CANDIDATE.value
        if current is not None:
            if current.track_id == keep_id:
                current.track_id = remove_id
                current.link_status = status
        else:
            values = {f: _parse_field(f, entry[f]) for f in _SOURCE_FIELDS}
            values.update(track_id=remove_id, link_status=status, updated_at=_now())
            db.execute(insert(TrackSource).values(**values))
        db.flush()

    # Playlist-Einträge und Importprotokoll zurückhängen
    if ids := [uuid.UUID(i) for i in snap.get("playlist_items", [])]:
        db.execute(
            update(PlaylistItem)
            .where(PlaylistItem.id.in_(ids), PlaylistItem.track_id == keep_id)
            .values(track_id=remove_id)
        )
    for column, key in (
        (ImportItem.match_track_id, "import_match_items"),
        (ImportItem.result_track_id, "import_result_items"),
    ):
        if ids := [uuid.UUID(i) for i in snap.get(key, [])]:
            db.execute(
                update(ImportItem)
                .where(ImportItem.id.in_(ids), column == keep_id)
                .values({column.key: remove_id})
            )

    # Kandidaten: Übertragungen zurücknehmen, gesicherte Paare wiederherstellen
    transferred = snap.get("candidates_transferred", {})
    if inserted := [uuid.UUID(i) for i in transferred.get("inserted", [])]:
        db.execute(delete(MatchCandidate).where(MatchCandidate.id.in_(inserted)))
    for row in transferred.get("updated", []):
        db.execute(
            update(MatchCandidate)
            .where(MatchCandidate.id == uuid.UUID(row["id"]))
            .values(
                status=row["status"],
                decided_at=_parse_field("decided_at", row["decided_at"]),
                decided_by=row["decided_by"],
            )
        )
    for row in snap.get("candidates_removed", []):
        values = {f: _parse_field(f, row[f]) for f in _CANDIDATE_FIELDS}
        if not db.get(Track, values["track_a_id"]) or not db.get(Track, values["track_b_id"]):
            continue
        exists = db.scalar(
            select(MatchCandidate.id).where(
                (MatchCandidate.id == values["id"])
                | (
                    (MatchCandidate.track_a_id == values["track_a_id"])
                    & (MatchCandidate.track_b_id == values["track_b_id"])
                )
            )
        )
        if exists is None:
            db.execute(insert(MatchCandidate).values(**values))
    db.flush()

    # Das Paar selbst als abgelehnt merken
    reject_pair(
        db,
        keep_id,
        remove_id,
        by=by,
        confidence=merge.confidence,
        level=merge.level,
        note="Zusammenführung rückgängig gemacht",
    )

    merge.status = MergeStatus.UNDONE.value
    merge.undone_at, merge.undone_by = _now(), by
    db.flush()
    db.expire_all()
    log.info("Zusammenführung rückgängig gemacht", extra={"merge_id": str(merge_id)})
    return merge


def _external_linked_elsewhere(
    db: Session, platform: str, external_id: str, exclude: uuid.UUID
) -> bool:
    return (
        db.scalar(
            select(TrackSource.id).where(
                TrackSource.platform == platform,
                TrackSource.external_id == external_id,
                TrackSource.link_status == LinkStatus.LINKED.value,
                TrackSource.id != exclude,
            )
        )
        is not None
    )


def reject_pair(
    db: Session,
    a: uuid.UUID,
    b: uuid.UUID,
    *,
    by: str,
    confidence: int | None = None,
    level: str | None = None,
    note: str | None = None,
) -> MatchCandidate:
    """Paar als „unterschiedlich“ merken (legt den Kandidaten bei Bedarf an)."""
    from musichub.matching.engine import ENGINE_VERSION

    first, second = ordered(a, b)
    cand = db.scalar(
        select(MatchCandidate)
        .where(MatchCandidate.track_a_id == first, MatchCandidate.track_b_id == second)
        .with_for_update()
    )
    if cand is None:
        cand = MatchCandidate(
            track_a_id=first,
            track_b_id=second,
            confidence=confidence or 0,
            level=level or MatchLevel.REVIEW.value,
            reasons=[],
            blockers=[],
            engine_version=ENGINE_VERSION,
        )
        db.add(cand)
    cand.status = CandidateStatus.REJECTED.value
    cand.decided_at, cand.decided_by = _now(), by
    if note:
        cand.reasons = [
            *[r for r in (cand.reasons or []) if r.get("code") != "user_note"],
            {"code": "user_note", "text": note, "effect": 0, "kind": "info"},
        ]
    db.flush()
    return cand
