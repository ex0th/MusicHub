"""Plattform-Konten und Importzustand

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-31 21:59:18.058905+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "connector_accounts",
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_user_id", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=255), nullable=False),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("connected_by", sa.String(length=255), nullable=False),
        sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("playlists_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_import_at", sa.DateTime(timezone=True), nullable=True),
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
            name=op.f("ck_connector_accounts_platform"),
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'reauth_required')", name=op.f("ck_connector_accounts_status")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_connector_accounts")),
        sa.UniqueConstraint("platform", name=op.f("uq_connector_accounts_platform")),
    )
    op.create_table(
        "oauth_states",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("code_verifier_enc", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')",
            name=op.f("ck_oauth_states_platform"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_oauth_states")),
        sa.UniqueConstraint("state_hash", name=op.f("uq_oauth_states_state_hash")),
    )
    op.create_table(
        "remote_playlists",
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=500), nullable=False),
        sa.Column("owner_name", sa.String(length=255), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("track_count", sa.Integer(), nullable=True),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("owned", sa.Boolean(), nullable=False),
        sa.Column("items_accessible", sa.Boolean(), nullable=False),
        sa.Column("remote_version", sa.String(length=255), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_playlist_id", sa.Uuid(), nullable=True),
        sa.Column("imported_version", sa.String(length=255), nullable=True),
        sa.Column("last_imported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_import_status", sa.String(length=16), nullable=True),
        sa.Column("last_import_run_id", sa.Uuid(), nullable=True),
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
            "last_import_status IS NULL OR last_import_status IN ('ok', 'unchanged', 'error')",
            name=op.f("ck_remote_playlists_last_import_status"),
        ),
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')",
            name=op.f("ck_remote_playlists_platform"),
        ),
        sa.ForeignKeyConstraint(
            ["last_import_run_id"],
            ["import_runs.id"],
            name=op.f("fk_remote_playlists_last_import_run_id_import_runs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["local_playlist_id"],
            ["playlists.id"],
            name=op.f("fk_remote_playlists_local_playlist_id_playlists"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remote_playlists")),
        sa.UniqueConstraint("platform", "external_id", name=op.f("uq_remote_playlists_platform")),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "progress",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "jobs", sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "result")
    op.drop_column("jobs", "progress")
    op.drop_table("remote_playlists")
    op.drop_table("oauth_states")
    op.drop_table("connector_accounts")
