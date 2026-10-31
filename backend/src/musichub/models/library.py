"""Die lokale Musiksammlung – führende Instanz aller Daten.

Ein Track existiert unabhängig davon, ob er auf irgendeiner Plattform verfügbar ist.
Plattform-Zuordnungen hängen als ``PlatformLink`` am Track, nie umgekehrt.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import LinkStatus, MatchMethod, Platform, sql_in


class Track(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "tracks"

    title: Mapped[str] = mapped_column(String(500))
    #: Anzeigeform der Interpreten ("A, B feat. C"). Eigene Artist-Tabelle folgt laut Roadmap.
    artist: Mapped[str] = mapped_column(String(500))
    album: Mapped[str | None] = mapped_column(String(500))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    isrc: Mapped[str | None] = mapped_column(String(12), index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    links: Mapped[list[PlatformLink]] = relationship(
        back_populates="track", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        CheckConstraint("duration_ms IS NULL OR duration_ms > 0", name="duration_positive"),
        CheckConstraint(
            "isrc IS NULL OR isrc ~ '^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$'", name="isrc_format"
        ),
    )


class PlatformLink(UUIDPrimaryKey, Timestamps, Base):
    """Zuordnung eines lokalen Tracks zu einem Track auf einer Streamingplattform."""

    __tablename__ = "platform_links"

    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(16), default=LinkStatus.CANDIDATE.value)
    match_method: Mapped[str] = mapped_column(String(16), default=MatchMethod.MANUAL.value)
    match_score: Mapped[float | None] = mapped_column(Float)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    track: Mapped[Track] = relationship(back_populates="links")

    __table_args__ = (
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(f"status IN ({sql_in(LinkStatus)})", name="status"),
        CheckConstraint(f"match_method IN ({sql_in(MatchMethod)})", name="match_method"),
        CheckConstraint("match_score IS NULL OR match_score BETWEEN 0 AND 1", name="score_range"),
        UniqueConstraint("track_id", "platform", "external_id"),
        # Pro Track und Plattform höchstens eine bestätigte Verknüpfung …
        Index(
            "uq_platform_links_one_linked_per_platform",
            "track_id",
            "platform",
            unique=True,
            postgresql_where=text("status = 'linked'"),
        ),
        # … und ein Plattform-Track gehört höchstens zu einem lokalen Track.
        Index(
            "uq_platform_links_external_linked_once",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=text("status = 'linked'"),
        ),
    )


class Playlist(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "playlists"

    name: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)

    items: Mapped[list[PlaylistItem]] = relationship(
        back_populates="playlist",
        cascade="all, delete-orphan",
        order_by="PlaylistItem.position",
    )
    targets: Mapped[list[PlaylistTarget]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan"
    )


class PlaylistItem(UUIDPrimaryKey, Base):
    __tablename__ = "playlist_items"

    playlist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    playlist: Mapped[Playlist] = relationship(back_populates="items")
    track: Mapped[Track] = relationship()

    __table_args__ = (
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_playlist_items_playlist_position", "playlist_id", "position"),
    )


class PlaylistTarget(UUIDPrimaryKey, Timestamps, Base):
    """Eine lokale Playlist wird auf eine Plattform-Playlist synchronisiert (lokal -> remote)."""

    __tablename__ = "playlist_targets"

    playlist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    #: None, solange die Plattform-Playlist noch nicht angelegt wurde.
    remote_playlist_id: Mapped[str | None] = mapped_column(String(255))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)

    playlist: Mapped[Playlist] = relationship(back_populates="targets")

    __table_args__ = (
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        UniqueConstraint("playlist_id", "platform"),
    )
