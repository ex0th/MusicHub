"""Matching: Vorschläge für doppelte Tracks und protokollierte Zusammenführungen."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import CandidateStatus, MatchLevel, MergeMethod, MergeStatus, sql_in


class MatchCandidate(UUIDPrimaryKey, Timestamps, Base):
    """Ein Paar lokaler Tracks, das dieselbe Aufnahme sein könnte.

    Das Paar ist ungeordnet und wird mit ``track_a_id < track_b_id`` gespeichert, damit es
    genau einmal existiert. Abgelehnte Paare bleiben als Gedächtnis erhalten und werden bei
    neuen Scans nicht erneut vorgeschlagen.
    """

    __tablename__ = "match_candidates"

    track_a_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    track_b_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    confidence: Mapped[int] = mapped_column(Integer)
    level: Mapped[str] = mapped_column(String(16))
    #: Begründung der Engine: [{code, text, effect, kind}]
    reasons: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    blockers: Mapped[list[str]] = mapped_column(JSONB, default=list)
    engine_version: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), default=CandidateStatus.OPEN.value)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        UniqueConstraint("track_a_id", "track_b_id"),
        CheckConstraint("track_a_id < track_b_id", name="ordered_pair"),
        CheckConstraint(f"level IN ({sql_in(MatchLevel)})", name="level"),
        CheckConstraint(f"status IN ({sql_in(CandidateStatus)})", name="status"),
        CheckConstraint("confidence BETWEEN 0 AND 100", name="confidence_range"),
        Index("ix_match_candidates_status_confidence", "status", "confidence"),
    )


class TrackMerge(UUIDPrimaryKey, Timestamps, Base):
    """Protokoll einer Zusammenführung mit allem, was zum Rückgängigmachen nötig ist.

    Track-IDs bewusst ohne Fremdschlüssel: Der entfernte Track existiert nach der
    Zusammenführung nicht mehr, und das Protokoll soll auch spätere Löschungen überdauern.
    """

    __tablename__ = "track_merges"

    kept_track_id: Mapped[uuid.UUID] = mapped_column(index=True)
    removed_track_id: Mapped[uuid.UUID] = mapped_column(index=True)
    kept_title: Mapped[str] = mapped_column(String(500))
    kept_artist: Mapped[str] = mapped_column(String(500))
    removed_title: Mapped[str] = mapped_column(String(500))
    removed_artist: Mapped[str] = mapped_column(String(500))
    method: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[int | None] = mapped_column(Integer)
    level: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default=MergeStatus.MERGED.value)
    #: Zustand vor der Zusammenführung und alle Änderungen (siehe library/merge.py)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_by: Mapped[str] = mapped_column(String(255))
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undone_by: Mapped[str | None] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint(f"method IN ({sql_in(MergeMethod)})", name="method"),
        CheckConstraint(f"status IN ({sql_in(MergeStatus)})", name="status"),
        Index("ix_track_merges_created_at", "created_at"),
    )
