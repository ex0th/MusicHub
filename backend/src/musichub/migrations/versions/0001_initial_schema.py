"""Initiales Schema: Tracks, Plattform-Links, Playlists, Sync-Ziele, Jobs

Revision ID: 0001
Revises:
Create Date: 2026-10-31 14:14:25.065631+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("kind", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column(
            "run_after", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'dead')",
            name=op.f("ck_jobs_status"),
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts > 0", name=op.f("ck_jobs_attempts")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index("ix_jobs_pickup", "jobs", ["status", "run_after"], unique=False)
    op.create_table(
        "playlists",
        sa.Column("name", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
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
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playlists")),
    )
    op.create_table(
        "tracks",
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("artist", sa.String(length=500), nullable=False),
        sa.Column("album", sa.String(length=500), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("isrc", sa.String(length=12), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "isrc IS NULL OR isrc ~ '^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$'",
            name=op.f("ck_tracks_isrc_format"),
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms > 0", name=op.f("ck_tracks_duration_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tracks")),
    )
    op.create_index(op.f("ix_tracks_isrc"), "tracks", ["isrc"], unique=False)
    op.create_table(
        "platform_links",
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("match_method", sa.String(length=16), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint(
            "match_method IN ('manual', 'isrc', 'fuzzy', 'import')",
            name=op.f("ck_platform_links_match_method"),
        ),
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')",
            name=op.f("ck_platform_links_platform"),
        ),
        sa.CheckConstraint(
            "status IN ('linked', 'candidate', 'rejected', 'unavailable')",
            name=op.f("ck_platform_links_status"),
        ),
        sa.CheckConstraint(
            "match_score IS NULL OR match_score BETWEEN 0 AND 1",
            name=op.f("ck_platform_links_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name=op.f("fk_platform_links_track_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_platform_links")),
        sa.UniqueConstraint(
            "track_id", "platform", "external_id", name=op.f("uq_platform_links_track_id")
        ),
    )
    op.create_index(
        op.f("ix_platform_links_track_id"), "platform_links", ["track_id"], unique=False
    )
    op.create_index(
        "uq_platform_links_external_linked_once",
        "platform_links",
        ["platform", "external_id"],
        unique=True,
        postgresql_where=sa.text("status = 'linked'"),
    )
    op.create_index(
        "uq_platform_links_one_linked_per_platform",
        "platform_links",
        ["track_id", "platform"],
        unique=True,
        postgresql_where=sa.text("status = 'linked'"),
    )
    op.create_table(
        "playlist_items",
        sa.Column("playlist_id", sa.Uuid(), nullable=False),
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("position >= 0", name=op.f("ck_playlist_items_position_non_negative")),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.id"],
            name=op.f("fk_playlist_items_playlist_id_playlists"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name=op.f("fk_playlist_items_track_id_tracks"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playlist_items")),
    )
    op.create_index(
        "ix_playlist_items_playlist_position",
        "playlist_items",
        ["playlist_id", "position"],
        unique=False,
    )
    op.create_index(
        op.f("ix_playlist_items_track_id"), "playlist_items", ["track_id"], unique=False
    )
    op.create_table(
        "playlist_targets",
        sa.Column("playlist_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("remote_playlist_id", sa.String(length=255), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')",
            name=op.f("ck_playlist_targets_platform"),
        ),
        sa.ForeignKeyConstraint(
            ["playlist_id"],
            ["playlists.id"],
            name=op.f("fk_playlist_targets_playlist_id_playlists"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_playlist_targets")),
        sa.UniqueConstraint(
            "playlist_id", "platform", name=op.f("uq_playlist_targets_playlist_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("playlist_targets")
    op.drop_index(op.f("ix_playlist_items_track_id"), table_name="playlist_items")
    op.drop_index("ix_playlist_items_playlist_position", table_name="playlist_items")
    op.drop_table("playlist_items")
    op.drop_index(
        "uq_platform_links_one_linked_per_platform",
        table_name="platform_links",
        postgresql_where=sa.text("status = 'linked'"),
    )
    op.drop_index(
        "uq_platform_links_external_linked_once",
        table_name="platform_links",
        postgresql_where=sa.text("status = 'linked'"),
    )
    op.drop_index(op.f("ix_platform_links_track_id"), table_name="platform_links")
    op.drop_table("platform_links")
    op.drop_index(op.f("ix_tracks_isrc"), table_name="tracks")
    op.drop_table("tracks")
    op.drop_table("playlists")
    op.drop_index("ix_jobs_pickup", table_name="jobs")
    op.drop_table("jobs")
