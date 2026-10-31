"""Importprotokoll: import_runs und import_items

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-31 15:36:43.881794+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "import_runs",
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("options", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("summary", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
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
            "status IN ('analyzed', 'committed', 'discarded', 'failed')",
            name=op.f("ck_import_runs_status"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_runs")),
    )
    op.create_index("ix_import_runs_created_at", "import_runs", ["created_at"], unique=False)
    op.create_index(op.f("ix_import_runs_sha256"), "import_runs", ["sha256"], unique=False)
    op.create_table(
        "import_items",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("row", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("match_track_id", sa.Uuid(), nullable=True),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("match_reason", sa.String(length=32), nullable=True),
        sa.Column("duplicate_of_seq", sa.Integer(), nullable=True),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("playlists", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=True),
        sa.Column("result_track_id", sa.Uuid(), nullable=True),
        sa.Column("result_message", sa.Text(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "decision IN ('create', 'use_existing', 'skip')", name=op.f("ck_import_items_decision")
        ),
        sa.CheckConstraint(
            "result IS NULL OR result IN ('created', 'linked', 'skipped', 'failed')",
            name=op.f("ck_import_items_result"),
        ),
        sa.CheckConstraint(
            "status IN ('new', 'existing', 'potential_duplicate', 'file_duplicate', 'invalid')",
            name=op.f("ck_import_items_status"),
        ),
        sa.ForeignKeyConstraint(
            ["match_track_id"],
            ["tracks.id"],
            name=op.f("fk_import_items_match_track_id_tracks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["result_track_id"],
            ["tracks.id"],
            name=op.f("fk_import_items_result_track_id_tracks"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["import_runs.id"],
            name=op.f("fk_import_items_run_id_import_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_import_items")),
    )
    op.create_index("ix_import_items_run_seq", "import_items", ["run_id", "seq"], unique=True)
    op.create_index(
        "ix_import_items_run_status", "import_items", ["run_id", "status"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_import_items_run_status", table_name="import_items")
    op.drop_index("ix_import_items_run_seq", table_name="import_items")
    op.drop_table("import_items")
    op.drop_index(op.f("ix_import_runs_sha256"), table_name="import_runs")
    op.drop_index("ix_import_runs_created_at", table_name="import_runs")
    op.drop_table("import_runs")
