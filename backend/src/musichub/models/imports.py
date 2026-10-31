"""Importprotokoll: jeder Importlauf mit allen Datensätzen, Entscheidungen und Ergebnissen."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import (
    ImportDecision,
    ImportItemStatus,
    ImportResult,
    ImportStatus,
    sql_in,
)


class ImportRun(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "import_runs"

    filename: Mapped[str] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    #: SHA-256 der hochgeladenen Bytes – erkennt erneut hochgeladene Dateien
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    #: erkanntes Format, z. B. "tunemymusic_csv" (leer, wenn die Datei abgelehnt wurde)
    format: Mapped[str | None] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    created_by: Mapped[str] = mapped_column(String(255))
    committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Playlist-Entscheidungen und Optionen des Assistenten
    options: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    #: Zählerstände nach Analyse bzw. Commit
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list[str]] = mapped_column(JSONB, default=list)
    error: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[ImportItem]] = relationship(
        back_populates="run", cascade="all, delete-orphan", order_by="ImportItem.seq"
    )

    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(ImportStatus)})", name="status"),
        Index("ix_import_runs_created_at", "created_at"),
    )


class ImportItem(UUIDPrimaryKey, Base):
    """Ein Datensatz der Datei mit Abgleichsergebnis, Entscheidung und Ergebnis."""

    __tablename__ = "import_items"

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("import_runs.id", ondelete="CASCADE"))
    #: laufende Nummer in der Datei (0-basiert), bestimmt die Verarbeitungsreihenfolge
    seq: Mapped[int] = mapped_column(Integer)
    #: Zeilennummer in der Datei für Meldungen
    row: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))
    decision: Mapped[str] = mapped_column(String(16))
    match_track_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL")
    )
    match_score: Mapped[float | None] = mapped_column(Float)
    match_reason: Mapped[str | None] = mapped_column(String(32))
    #: seq des ersten Vorkommens desselben Tracks in der Datei
    duplicate_of_seq: Mapped[int | None] = mapped_column(Integer)
    #: normalisierter Datensatz (ImportRecord.to_dict())
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    #: Playlists der Datei, in denen dieser Datensatz vorkommt
    playlists: Mapped[list[str]] = mapped_column(JSONB, default=list)
    result: Mapped[str | None] = mapped_column(String(16))
    result_track_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tracks.id", ondelete="SET NULL")
    )
    result_message: Mapped[str | None] = mapped_column(Text)

    run: Mapped[ImportRun] = relationship(back_populates="items")

    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(ImportItemStatus)})", name="status"),
        CheckConstraint(f"decision IN ({sql_in(ImportDecision)})", name="decision"),
        CheckConstraint(f"result IS NULL OR result IN ({sql_in(ImportResult)})", name="result"),
        Index("ix_import_items_run_seq", "run_id", "seq", unique=True),
        Index("ix_import_items_run_status", "run_id", "status"),
    )
