"""Bibliothek: Track-Version/Veröffentlichung, track_sources, Tags, eindeutige Positionen

* ``tracks``: neue Spalten ``version`` und ``release_date``, Index (artist, title).
* ``platform_links`` wird zu ``track_sources``: Verknüpfungsstatus (``link_status``) und
  Verfügbarkeit (``availability``) sind jetzt getrennte Felder. Bestehende Daten werden
  übernommen; der frühere Status ``unavailable`` wird zu ``linked`` + ``unavailable``.
* ``tags`` und ``track_tags`` (n:m).
* ``playlist_items``: (playlist_id, position) eindeutig, Prüfung erst beim Commit.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31 14:52:37.792528+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamps() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    # --- tracks ------------------------------------------------------------------
    op.add_column("tracks", sa.Column("version", sa.String(length=200), nullable=True))
    op.add_column("tracks", sa.Column("release_date", sa.Date(), nullable=True))
    op.create_index("ix_tracks_artist_title", "tracks", ["artist", "title"], unique=False)

    # --- tags --------------------------------------------------------------------
    op.create_table(
        "tags",
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("category", sa.String(length=50), server_default="Allgemein", nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tags")),
    )
    op.create_index(
        "uq_tags_category_name",
        "tags",
        [sa.literal_column("lower(category)"), sa.literal_column("lower(name)")],
        unique=True,
    )
    op.create_table(
        "track_tags",
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("tag_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tag_id"], ["tags.id"], name=op.f("fk_track_tags_tag_id_tags"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name=op.f("fk_track_tags_track_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("track_id", "tag_id", name=op.f("pk_track_tags")),
    )
    op.create_index(op.f("ix_track_tags_tag_id"), "track_tags", ["tag_id"], unique=False)

    # --- track_sources (ersetzt platform_links) ----------------------------------
    op.create_table(
        "track_sources",
        sa.Column("track_id", sa.Uuid(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("url", sa.String(length=1000), nullable=True),
        sa.Column("link_status", sa.String(length=16), nullable=False),
        sa.Column("availability", sa.String(length=16), server_default="unknown", nullable=False),
        sa.Column("match_method", sa.String(length=16), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "availability IN ('unknown', 'available', 'unavailable')",
            name=op.f("ck_track_sources_availability"),
        ),
        sa.CheckConstraint(
            "link_status IN ('linked', 'candidate', 'rejected')",
            name=op.f("ck_track_sources_link_status"),
        ),
        sa.CheckConstraint(
            "match_method IN ('manual', 'isrc', 'fuzzy', 'import')",
            name=op.f("ck_track_sources_match_method"),
        ),
        sa.CheckConstraint(
            "platform IN ('spotify', 'soundcloud', 'youtube')",
            name=op.f("ck_track_sources_platform"),
        ),
        sa.CheckConstraint(
            "match_score IS NULL OR match_score BETWEEN 0 AND 1",
            name=op.f("ck_track_sources_score_range"),
        ),
        sa.ForeignKeyConstraint(
            ["track_id"],
            ["tracks.id"],
            name=op.f("fk_track_sources_track_id_tracks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_track_sources")),
        sa.UniqueConstraint(
            "track_id", "platform", "external_id", name=op.f("uq_track_sources_track_id")
        ),
    )
    op.create_index(op.f("ix_track_sources_track_id"), "track_sources", ["track_id"], unique=False)

    # Daten übernehmen. "unavailable" war bisher ein Verknüpfungsstatus; jetzt wird daraus
    # eine bestätigte Quelle mit Verfügbarkeit "unavailable" – außer es gibt für Track und
    # Plattform schon eine bestätigte Quelle, dann bleibt sie Kandidat.
    op.execute(
        """
        INSERT INTO track_sources (id, track_id, platform, external_id, url, link_status,
                                   availability, match_method, match_score, last_checked_at,
                                   created_at, updated_at)
        SELECT pl.id, pl.track_id, pl.platform, pl.external_id, pl.url,
               CASE
                 WHEN pl.status <> 'unavailable' THEN pl.status
                 WHEN EXISTS (SELECT 1 FROM platform_links o
                              WHERE o.track_id = pl.track_id AND o.platform = pl.platform
                                AND o.status = 'linked') THEN 'candidate'
                 ELSE 'linked'
               END,
               CASE WHEN pl.status = 'unavailable' THEN 'unavailable' ELSE 'unknown' END,
               pl.match_method, pl.match_score, pl.last_checked_at, pl.created_at, pl.updated_at
        FROM platform_links pl
        """
    )
    op.create_index(
        "uq_track_sources_external_linked_once",
        "track_sources",
        ["platform", "external_id"],
        unique=True,
        postgresql_where=sa.text("link_status = 'linked'"),
    )
    op.create_index(
        "uq_track_sources_one_linked_per_platform",
        "track_sources",
        ["track_id", "platform"],
        unique=True,
        postgresql_where=sa.text("link_status = 'linked'"),
    )
    op.drop_table("platform_links")  # Indizes und Constraints fallen mit

    # --- playlist_items: lückenlose, eindeutige Positionen ------------------------
    op.execute(
        """
        UPDATE playlist_items pi SET position = n.rn
        FROM (SELECT id, row_number() OVER (PARTITION BY playlist_id
                                            ORDER BY position, added_at, id) - 1 AS rn
              FROM playlist_items) n
        WHERE pi.id = n.id AND pi.position <> n.rn
        """
    )
    op.drop_index(op.f("ix_playlist_items_playlist_position"), table_name="playlist_items")
    op.create_unique_constraint(
        "uq_playlist_items_playlist_position",
        "playlist_items",
        ["playlist_id", "position"],
        deferrable=True,
        initially="DEFERRED",
    )


def downgrade() -> None:
    op.drop_constraint("uq_playlist_items_playlist_position", "playlist_items", type_="unique")
    op.create_index(
        op.f("ix_playlist_items_playlist_position"),
        "playlist_items",
        ["playlist_id", "position"],
        unique=False,
    )

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
        *_timestamps(),
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
    op.execute(
        """
        INSERT INTO platform_links (id, track_id, platform, external_id, url, status,
                                    match_method, match_score, last_checked_at,
                                    created_at, updated_at)
        SELECT id, track_id, platform, external_id, url,
               CASE WHEN link_status = 'linked' AND availability = 'unavailable'
                    THEN 'unavailable' ELSE link_status END,
               match_method, match_score, last_checked_at, created_at, updated_at
        FROM track_sources
        """
    )
    op.create_index(
        op.f("ix_platform_links_track_id"), "platform_links", ["track_id"], unique=False
    )
    op.create_index(
        "uq_platform_links_one_linked_per_platform",
        "platform_links",
        ["track_id", "platform"],
        unique=True,
        postgresql_where=sa.text("status = 'linked'"),
    )
    op.create_index(
        "uq_platform_links_external_linked_once",
        "platform_links",
        ["platform", "external_id"],
        unique=True,
        postgresql_where=sa.text("status = 'linked'"),
    )
    op.drop_table("track_sources")
    op.drop_table("track_tags")
    op.drop_index("uq_tags_category_name", table_name="tags")
    op.drop_table("tags")

    op.drop_index("ix_tracks_artist_title", table_name="tracks")
    op.drop_column("tracks", "release_date")
    op.drop_column("tracks", "version")
