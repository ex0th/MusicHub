"""Verbundene Plattform-Konten, laufende OAuth-Anmeldungen und Playlists der Plattformen."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from musichub.db.base import Base, Timestamps, UUIDPrimaryKey
from musichub.domain.types import AccountStatus, Platform, RemoteImportStatus, sql_in


class ConnectorAccount(UUIDPrimaryKey, Timestamps, Base):
    """Ein verbundenes Konto je Plattform (MusicHub ist eine Instanz pro Haushalt/Nutzer).

    Tokens liegen **nur verschlüsselt** vor (``connections/crypto.py``) und verlassen den
    Server nie – weder in API-Antworten noch in Logs.
    """

    __tablename__ = "connector_accounts"

    platform: Mapped[str] = mapped_column(String(32), unique=True)
    external_user_id: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    scopes: Mapped[str | None] = mapped_column(Text)
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(24), default=AccountStatus.CONNECTED.value)
    last_error: Mapped[str | None] = mapped_column(Text)
    connected_by: Mapped[str] = mapped_column(String(255))
    connected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: letzte erfolgreiche Aktualisierung der Playlist-Liste
    playlists_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: letzter erfolgreicher Import einer Playlist
    last_import_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(f"status IN ({sql_in(AccountStatus)})", name="status"),
    )


class OAuthState(UUIDPrimaryKey, Base):
    """Laufende Anmeldung: einmaliger ``state`` (nur als SHA-256 gespeichert) und der
    verschlüsselte PKCE-Verifier. Läuft nach 10 Minuten ab."""

    __tablename__ = "oauth_states"

    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    platform: Mapped[str] = mapped_column(String(32))
    code_verifier_enc: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),)


class RemotePlaylist(UUIDPrimaryKey, Timestamps, Base):
    """Eine Playlist auf der Plattform (Metadaten) und ihr Importzustand."""

    __tablename__ = "remote_playlists"

    platform: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    owner_name: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    track_count: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(String(1000))
    owned: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Inhalte lesbar (Spotify Development Mode: nur eigene/kollaborative Playlists)
    items_accessible: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Versionsmerkmal der Plattform (snapshot_id, ETag, last_modified)
    remote_version: Mapped[str | None] = mapped_column(String(255))
    #: zuletzt in der Playlist-Liste gesehen; ``removed_at`` = nicht mehr vorhanden
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_playlist_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("playlists.id", ondelete="SET NULL")
    )
    imported_version: Mapped[str | None] = mapped_column(String(255))
    last_imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_import_status: Mapped[str | None] = mapped_column(String(16))
    last_import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("import_runs.id", ondelete="SET NULL")
    )
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        UniqueConstraint("platform", "external_id"),
        CheckConstraint(f"platform IN ({sql_in(Platform)})", name="platform"),
        CheckConstraint(
            f"last_import_status IS NULL OR last_import_status IN ({sql_in(RemoteImportStatus)})",
            name="last_import_status",
        ),
    )
