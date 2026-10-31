"""Persistente Job-Queue in PostgreSQL (SELECT … FOR UPDATE SKIP LOCKED).

Kein Redis/Celery nötig: Die Datenbank ist ohnehin da, Jobs überleben Neustarts, und
der Zustand ist per SQL einsehbar.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import JobStatus, sql_in


class Job(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "jobs"

    kind: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Fortschritt laufender Jobs: {"current", "total", "message"} – eigene Transaktion
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    #: Ergebnis erfolgreicher Jobs (Zähler, Hinweise)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    __table_args__ = (
        CheckConstraint(f"status IN ({sql_in(JobStatus)})", name="status"),
        CheckConstraint("attempts >= 0 AND max_attempts > 0", name="attempts"),
        Index("ix_jobs_pickup", "status", "run_after"),
    )
