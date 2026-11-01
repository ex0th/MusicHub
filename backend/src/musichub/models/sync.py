"""Synchronisationshistorie und Suchergebnisse auf Zielplattformen."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import (
    LookupStatus,
    Platform,
    SyncMode,
    SyncRunStatus,
    SyncTrigger,
    sql_in,
)


class SyncRun(UUIDPrimaryKey, Timestamps, Base):
    """Ein Synchronisationslauf (auch Vorschau/Dry-Run) mit Plan und Ergebnis.

    Bleibt erhalten, wenn Ziel oder Playlist gelöscht werden (Fremdschlüssel SET NULL,
    Namen werden mitgeschrieben)."""

    __tablename__ = "sync_runs"

    target_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("playlist_targets.id", ondelete="SET NULL")
    )
    playlist_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("playlists.id", ondelete="SET NULL")
    )
    playlist_name: Mapped[str] = mapped_column(String(300))
    platform: Mapped[str] = mapped_column(String(32))
    remote_playlist_id: Mapped[str | None] = mapped_column(String(255))
    remote_name: Mapped[str | None] = mapped_column(String(500))
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    dry_run: Mapped[bool] = mapped_column(Boolean)
    mode: Mapped[str] = mapped_column(String(16))
    #: externe Tracks durften entfernt werden (ausdrückliche Zustimmung)
    approved_external_removal: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    added: Mapped[int] = mapped_column(Integer, default=0)
    removed: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    unmatched: Mapped[int] = mapped_column(Integer, default=0)
    #: Plan/Vorschau: Hinzufügen, Entfernen, nicht zugeordnete Tracks, externe Änderungen
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    errors: Mapped[list[str]] = mapped_column(JSONB, default=list)
    #: Vorschau, auf die sich dieser Lauf beruft (Bestätigung)
    preview_run_id: Mapped[uuid.UUID | None] = mapped_column()
    job_id: Mapped[uuid.UUID | None] = mapped_column()
    created_by: Mapped[str] = mapped_column(String(255))

    __table_args__ = (
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(f"trigger IN ({sql_in(SyncTrigger)})", name="trigger"),
        CheckConstraint(f"status IN ({sql_in(SyncRunStatus)})", name="status"),
        CheckConstraint(f"mode IN ({sql_in(SyncMode)})", name="mode"),
        Index("ix_sync_runs_target_created", "target_id", "created_at"),
        Index("ix_sync_runs_playlist_created", "playlist_id", "created_at"),
    )


class PlatformLookup(UUIDPrimaryKey, Base):
    """Ergebnis der Suche eines lokalen Tracks ohne Verknüpfung auf einer Plattform.

    Kein Eintrag = „noch nicht geprüft“. Wird beim Sync erneut versucht, wenn der Status
    ``api_error`` ist oder der Nutzer „erneut prüfen“ wählt (Eintrag wird gelöscht)."""

    __tablename__ = "platform_lookups"

    track_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tracks.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))
    #: bei found/candidate: gefundene externe ID
    external_id: Mapped[str | None] = mapped_column(String(255))
    confidence: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("track_id", "platform"),
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(f"status IN ({sql_in(LookupStatus)})", name="status"),
    )
