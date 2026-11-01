"""Die lokale Musiksammlung – führende Instanz aller Daten.

Ein Track existiert unabhängig davon, ob er auf irgendeiner Plattform verfügbar ist.
Plattform-Zuordnungen hängen als ``TrackSource`` am Track, nie umgekehrt.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import Availability, LinkStatus, MatchMethod, Platform, SyncMode, sql_in

#: n:m Tracks <-> Tags. Löschen eines Tracks oder Tags entfernt nur die Zuordnung.
track_tags = Table(
    "track_tags",
    Base.metadata,
    Column("track_id", ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True),
    Column("tag_id", ForeignKey("tags.id", ondelete="CASCADE"), primary_key=True, index=True),
)


class Track(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "tracks"

    title: Mapped[str] = mapped_column(String(500))
    #: Anzeigeform der Interpreten ("A, B feat. C"). Eigene Artist-Tabelle folgt laut Roadmap.
    artist: Mapped[str] = mapped_column(String(500))
    album: Mapped[str | None] = mapped_column(String(500))
    #: Version/Remix, z. B. "Extended Mix", "Live", "Radio Edit". Leer = Originalversion.
    version: Mapped[str | None] = mapped_column(String(200))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    isrc: Mapped[str | None] = mapped_column(String(12), index=True)
    release_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str | None] = mapped_column(Text)

    sources: Mapped[list[TrackSource]] = relationship(
        back_populates="track",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TrackSource.platform",
    )
    tags: Mapped[list[Tag]] = relationship(
        secondary=track_tags, back_populates="tracks", lazy="selectin", order_by="Tag.name"
    )

    __table_args__ = (
        CheckConstraint("duration_ms IS NULL OR duration_ms > 0", name="duration_positive"),
        CheckConstraint(
            "isrc IS NULL OR isrc ~ '^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$'", name="isrc_format"
        ),
        Index("ix_tracks_artist_title", "artist", "title"),
    )


class TrackSource(UUIDPrimaryKey, Timestamps, Base):
    """Ein Track auf einer Streamingplattform, der einem lokalen Track zugeordnet ist."""

    __tablename__ = "track_sources"

    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), index=True
    )
    platform: Mapped[str] = mapped_column(String(32))
    #: Plattforminterne ID (Spotify-Track-ID, YouTube-Video-ID, SoundCloud-Permalink).
    external_id: Mapped[str] = mapped_column(String(255))
    url: Mapped[str | None] = mapped_column(String(1000))
    link_status: Mapped[str] = mapped_column(String(16), default=LinkStatus.LINKED.value)
    availability: Mapped[str] = mapped_column(
        String(16), default=Availability.UNKNOWN.value, server_default=Availability.UNKNOWN.value
    )
    match_method: Mapped[str] = mapped_column(String(16), default=MatchMethod.MANUAL.value)
    match_score: Mapped[float | None] = mapped_column(Float)
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    track: Mapped[Track] = relationship(back_populates="sources")

    __table_args__ = (
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(f"link_status IN ({sql_in(LinkStatus)})", name="link_status"),
        CheckConstraint(f"availability IN ({sql_in(Availability)})", name="availability"),
        CheckConstraint(f"match_method IN ({sql_in(MatchMethod)})", name="match_method"),
        CheckConstraint("match_score IS NULL OR match_score BETWEEN 0 AND 1", name="score_range"),
        UniqueConstraint("track_id", "platform", "external_id"),
        # Pro Track und Plattform höchstens eine bestätigte Quelle (eindeutiges Sync-Ziel) …
        Index(
            "uq_track_sources_one_linked_per_platform",
            "track_id",
            "platform",
            unique=True,
            postgresql_where=text("link_status = 'linked'"),
        ),
        # … und ein Plattform-Track gehört höchstens zu einem lokalen Track.
        Index(
            "uq_track_sources_external_linked_once",
            "platform",
            "external_id",
            unique=True,
            postgresql_where=text("link_status = 'linked'"),
        ),
    )


class Tag(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "tags"

    name: Mapped[str] = mapped_column(String(100))
    #: Freie Gruppierung, z. B. "Genre", "Stimmung", "Anlass".
    category: Mapped[str] = mapped_column(
        String(50), default="Allgemein", server_default="Allgemein"
    )

    tracks: Mapped[list[Track]] = relationship(secondary=track_tags, back_populates="tags")

    __table_args__ = (
        # Groß-/Kleinschreibung zählt nicht: "House" und "house" in derselben Kategorie = Duplikat
        Index("uq_tags_category_name", func.lower(category), func.lower(name), unique=True),
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
    """Ein Eintrag einer Playlist. Positionen sind lückenlos 0..n-1.

    Die Eindeutigkeit von (playlist_id, position) wird erst beim Commit geprüft
    (DEFERRABLE), damit Verschieben ohne Zwischenzustände möglich ist.
    """

    __tablename__ = "playlist_items"

    playlist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="RESTRICT"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    playlist: Mapped[Playlist] = relationship(back_populates="items")
    track: Mapped[Track] = relationship(lazy="joined", innerjoin=True)

    __table_args__ = (
        CheckConstraint("position >= 0", name="position_non_negative"),
        UniqueConstraint(
            "playlist_id",
            "position",
            name="uq_playlist_items_playlist_position",
            deferrable=True,
            initially="DEFERRED",
        ),
    )


class PlaylistTarget(UUIDPrimaryKey, Timestamps, Base):
    """Ein Synchronisationsziel: lokale Playlist → Playlist auf einer Plattform.

    Eine Playlist kann mehrere Ziele haben (auch mehrere je Plattform); eine
    Plattform-Playlist ist höchstens Ziel einer lokalen Playlist.
    """

    __tablename__ = "playlist_targets"

    playlist_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("playlists.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(String(32))
    #: None, solange die Plattform-Playlist noch nicht angelegt wurde.
    remote_playlist_id: Mapped[str | None] = mapped_column(String(255))
    remote_name: Mapped[str | None] = mapped_column(String(500))
    remote_url: Mapped[str | None] = mapped_column(String(1000))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    mode: Mapped[str] = mapped_column(
        String(16), default=SyncMode.ADD_ONLY.value, server_default=SyncMode.ADD_ONLY.value
    )
    #: von MusicHub angelegt – Voraussetzung für „vollständig abgleichen“
    managed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    #: ausdrücklich für „vollständig abgleichen“ freigegeben
    mirror_allowed: Mapped[bool] = mapped_column(default=False, server_default=text("false"))
    #: geplante Synchronisierung alle n Minuten (None = nur manuell)
    schedule_minutes: Mapped[int | None] = mapped_column(Integer)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: erste schreibende Synchronisierung bestätigt (Vorschau gesehen)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_by: Mapped[str | None] = mapped_column(String(255))
    #: externe IDs auf der Zielplaylist nach dem letzten erfolgreichen Sync
    #: (Grundlage für das Erkennen externer Änderungen)
    known_remote_items: Mapped[list[str] | None] = mapped_column(JSONB)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    last_run_id: Mapped[uuid.UUID | None] = mapped_column()
    last_error: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(255))

    playlist: Mapped[Playlist] = relationship(back_populates="targets")

    __table_args__ = (
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(f"mode IN ({sql_in(SyncMode)})", name="mode"),
        CheckConstraint(
            "mode <> 'mirror' OR (managed AND mirror_allowed)", name="mirror_only_managed"
        ),
        CheckConstraint(
            "schedule_minutes IS NULL OR schedule_minutes >= 60", name="schedule_minutes"
        ),
        Index(
            "uq_playlist_targets_remote",
            "platform",
            "remote_playlist_id",
            unique=True,
            postgresql_where=text("remote_playlist_id IS NOT NULL"),
        ),
        Index("ix_playlist_targets_playlist_id", "playlist_id"),
    )
