"""Matching: match_candidates und track_merges

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-31 16:13:31.221807+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "track_merges",
        sa.Column("kept_track_id", sa.Uuid(), nullable=False),
        sa.Column("removed_track_id", sa.Uuid(), nullable=False),
        sa.Column("kept_title", sa.String(length=500), nullable=False),
        sa.Column("kept_artist", sa.String(length=500), nullable=False),
        sa.Column("removed_title", sa.String(length=500), nullable=False),
        sa.Column("removed_artist", sa.String(length=500), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("undone_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("undone_by", sa.String(length=255), nullable=True),
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
        sa.CheckConstraint("method IN ('manual', 'auto')", name=op.f("ck_track_merges_method")),
        sa.CheckConstraint("status IN ('merged', 'undone')", name=op.f("ck_track_merges_status")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_track_merges")),
    )
    op.create_index("ix_track_merges_created_at", "track_merges", ["created_at"], unique=False)
    op.create_index(
        op.f("ix_track_merges_kept_track_id"), "track_merges", ["kept_track_id"], unique=False
    )
    op.create_index(
        op.f("ix_track_merges_removed_track_id"), "track_merges", ["removed_track_id"], unique=False
    )
    op.create_table(
        "match_candidates",
        sa.Column("track_a_id", sa.Uuid(), nullable=False),
        sa.Column("track_b_id", sa.Uuid(), nullable=False),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blockers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("engine_version", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.String(length=255), nullable=True),
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
            "level IN ('certain', 'likely', 'review', 'different')",
            name=op.f("ck_match_candidates_level"),
        ),
        sa.CheckConstraint(
            "status IN ('open', 'deferred', 'rejected')", name=op.f("ck_match_candidates_status")
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 100", name=op.f("ck_match_candidates_confidence_range")
        ),
        sa.CheckConstraint(
            "track_a_id < track_b_id", name=op.f("ck_match_candidates_ordered_pair")
        ),
        sa.ForeignKeyConstraint(
            ["track_a_id"],
            ["tracks.id"],
            name=op.f("fk_match_candidates_track_a_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["track_b_id"],
            ["tracks.id"],
            name=op.f("fk_match_candidates_track_b_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_match_candidates")),
        sa.UniqueConstraint(
            "track_a_id", "track_b_id", name=op.f("uq_match_candidates_track_a_id")
        ),
    )
    op.create_index(
        "ix_match_candidates_status_confidence",
        "match_candidates",
        ["status", "confidence"],
        unique=False,
    )
    op.create_index(
        op.f("ix_match_candidates_track_b_id"), "match_candidates", ["track_b_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_match_candidates_track_b_id"), table_name="match_candidates")
    op.drop_index("ix_match_candidates_status_confidence", table_name="match_candidates")
    op.drop_table("match_candidates")
    op.drop_index(op.f("ix_track_merges_removed_track_id"), table_name="track_merges")
    op.drop_index(op.f("ix_track_merges_kept_track_id"), table_name="track_merges")
    op.drop_index("ix_track_merges_created_at", table_name="track_merges")
    op.drop_table("track_merges")
