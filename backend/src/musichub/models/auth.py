"""Anmeldungen an MusicHub: laufende OIDC-Logins und serverseitige Sitzungen."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Index, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from musichub.db.base import Base, UUIDPrimaryKey


class AuthLogin(UUIDPrimaryKey, Base):
    """Laufender Login (Authorization Code Flow). Einmalig verwendbar, 10 Minuten gültig.

    ``state`` und das Bindungs-Cookie des Browsers liegen nur als SHA-256 vor. Der
    PKCE-Verifier wird aus dem Bindungs-Cookie abgeleitet und nie gespeichert.
    """

    __tablename__ = "auth_logins"

    state_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str] = mapped_column(String(64))
    nonce: Mapped[str] = mapped_column(String(128))
    return_to: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class UserSession(UUIDPrimaryKey, Base):
    """Serverseitige Sitzung. Der Browser hält nur ein zufälliges Token im
    HttpOnly-Cookie; gespeichert wird dessen SHA-256."""

    __tablename__ = "user_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    subject: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    groups: Mapped[list[str]] = mapped_column(JSONB, default=list)
    can_write: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    #: absolutes Ende, unabhängig von Aktivität
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_user_sessions_subject", "subject"),)
