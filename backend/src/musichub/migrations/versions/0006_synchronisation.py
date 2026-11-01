"""Synchronisation

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-01 09:15:33.212532+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_lookups",
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')",
            name=op.f("ck_platform_lookups_platform"),
        ),
        sa.CheckConstraint(
            "status IN ('found', 'candidate', 'not_found', 'api_error')",
            name=op.f("ck_platform_lookups_status"),
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name=op.f("fk_platform_lookups_track_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_lookups")),
        sa.UniqueConstraint("track_id", "platform", name=op.f("uq_platform_lookups_track_id")),
    )
    op.create_table(
        "sync_runs",
        sa.Column("target_id", sa.Uuid(), nullable=True),
        sa.Column("playlist_id", sa.Uuid(), nullable=True),
        sa.Column("playlist_name", sa.String(length=300), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("remote_playlist_id", sa.String(length=255), nullable=True),
        sa.Column("remote_name", sa.String(length=500), nullable=True),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("approved_external_removal", sa.Boolean(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("added", sa.Integer(), nullable=False),
        sa.Column("removed", sa.Integer(), nullable=False),
        sa.Column("skipped", sa.Integer(), nullable=False),
        sa.Column("unmatched", sa.Integer(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("preview_run_id", sa.Uuid(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("mode IN ('add_only', 'mirror')", name=op.f("ck_sync_runs_mode")),
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')", name=op.f("ck_sync_runs_platform")
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'preview', 'succeeded', 'no_changes', "
            "'partial', 'conflict', 'blocked', 'failed')",
            name=op.f("ck_sync_runs_status"),
        ),
        sa.CheckConstraint(
            "trigger IN ('preview', 'manual', 'scheduled')", name=op.f("ck_sync_runs_trigger")
        ),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.id"],
            name=op.f("fk_sync_runs_playlist_id_playlists"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["playlist_targets.id"],
            name=op.f("fk_sync_runs_target_id_playlist_targets"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_runs")),
    )
    op.create_index(
        "ix_sync_runs_playlist_created", "sync_runs", ["playlist_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_sync_runs_target_created", "sync_runs", ["target_id", "created_at"], unique=False
    )
    op.add_column(
        "oauth_states",
        sa.Column("access", sa.String(length=8), server_default="read", nullable=False),
    )
    op.add_column(
        "playlist_targets", sa.Column("remote_name", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "playlist_targets", sa.Column("remote_url", sa.String(length=1000), nullable=True)
    )
    op.add_column(
        "playlist_targets",
        sa.Column("mode", sa.String(length=16), server_default="add_only", nullable=False),
    )
    op.add_column(
        "playlist_targets",
        sa.Column("managed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column(
        "playlist_targets",
        sa.Column("mirror_allowed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
    op.add_column("playlist_targets", sa.Column("schedule_minutes", sa.Integer(), nullable=True))
    op.add_column(
        "playlist_targets", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "playlist_targets", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "playlist_targets", sa.Column("confirmed_by", sa.String(length=255), nullable=True)
    )
    op.add_column(
        "playlist_targets",
        sa.Column("known_remote_items", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("playlist_targets", sa.Column("last_status", sa.String(length=16), nullable=True))
    op.add_column("playlist_targets", sa.Column("last_run_id", sa.Uuid(), nullable=True))
    op.add_column("playlist_targets", sa.Column("created_by", sa.String(length=255), nullable=True))
    op.drop_constraint(op.f("uq_playlist_targets_playlist_id"), "playlist_targets", type_="unique")
    op.create_check_constraint(
        op.f("ck_playlist_targets_mode"), "playlist_targets", "mode IN ('add_only', 'mirror')"
    )
    op.create_check_constraint(
        op.f("ck_playlist_targets_mirror_only_managed"),
        "playlist_targets",
        "mode <> 'mirror' OR (managed AND mirror_allowed)",
    )
    op.create_check_constraint(
        op.f("ck_playlist_targets_schedule_minutes"),
        "playlist_targets",
        "schedule_minutes IS NULL OR schedule_minutes >= 60",
    )
    op.create_index(
        "ix_playlist_targets_playlist_id", "playlist_targets", ["playlist_id"], unique=False
    )
    op.create_index(
        "uq_playlist_targets_remote",
        "playlist_targets",
        ["platform", "remote_playlist_id"],
        unique=True,
        postgresql_where=sa.text("remote_playlist_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_playlist_targets_remote",
        table_name="playlist_targets",
        postgresql_where=sa.text("remote_playlist_id IS NOT NULL"),
    )
    op.drop_index("ix_playlist_targets_playlist_id", table_name="playlist_targets")
    for name in ("mode", "mirror_only_managed", "schedule_minutes"):
        op.drop_constraint(op.f(f"ck_playlist_targets_{name}"), "playlist_targets", type_="check")
    op.create_unique_constraint(
        op.f("uq_playlist_targets_playlist_id"),
        "playlist_targets",
        ["playlist_id", "platform"],
        postgresql_nulls_not_distinct=False,
    )
    op.drop_column("playlist_targets", "created_by")
    op.drop_column("playlist_targets", "last_run_id")
    op.drop_column("playlist_targets", "last_status")
    op.drop_column("playlist_targets", "known_remote_items")
    op.drop_column("playlist_targets", "confirmed_by")
    op.drop_column("playlist_targets", "confirmed_at")
    op.drop_column("playlist_targets", "next_run_at")
    op.drop_column("playlist_targets", "schedule_minutes")
    op.drop_column("playlist_targets", "mirror_allowed")
    op.drop_column("playlist_targets", "managed")
    op.drop_column("playlist_targets", "mode")
    op.drop_column("playlist_targets", "remote_url")
    op.drop_column("playlist_targets", "remote_name")
    op.drop_column("oauth_states", "access")
    op.drop_index("ix_sync_runs_target_created", table_name="sync_runs")
    op.drop_index("ix_sync_runs_playlist_created", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_table("platform_lookups")
