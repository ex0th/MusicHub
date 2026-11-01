"""Serverseitige Sitzungen für die Browser-Anmeldung (OIDC).

* Sitzungs-Cookie: zufälliges Token (256 Bit), ``HttpOnly``, ``Secure``, ``SameSite=Lax``,
  Präfix ``__Host-`` (nur über HTTPS). In der Datenbank liegt nur der SHA-256-Hash.
* Ablauf: Leerlauf (``MUSICHUB_SESSION_IDLE_MINUTES``) und absolute Höchstdauer
  (``MUSICHUB_SESSION_MAX_HOURS``). Abgelaufene Sitzungen werden gelöscht, die Antwort
  ist ``401`` mit ``code = session_expired`` und löscht die Cookies.
* CSRF: Jede verändernde Anfrage mit Sitzungs-Cookie braucht den Header
  ``X-CSRF-Token``. Der Wert ist ein HMAC des Sitzungs-Tokens und steht zusätzlich in
  einem für JavaScript lesbaren Cookie (``SameSite=Strict``). Ein fremder ``Origin``
  wird abgelehnt.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.orm import Session
from starlette.requests import Request
from starlette.responses import Response

from musichub.auth.principal import Principal
from musichub.core.config import Settings
from musichub.core.errors import ForbiddenError, UnauthorizedError
from musichub.models.auth import AuthLogin, UserSession

log = logging.getLogger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
LOGIN_TTL = timedelta(minutes=10)
#: Aktivität wird höchstens so oft gespeichert (spart Schreibzugriffe je Anfrage)
TOUCH_INTERVAL = timedelta(seconds=60)


class SessionExpiredError(UnauthorizedError):
    code = "session_expired"


def _now() -> datetime:
    return datetime.now(UTC)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _hmac(key: str, label: bytes) -> str:
    digest = hmac.new(key.encode("utf-8"), label, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def csrf_token_for(session_token: str) -> str:
    return _hmac(session_token, b"musichub-csrf")


def pkce_verifier_for(browser_token: str) -> str:
    """PKCE-Verifier (43 Zeichen) aus dem Bindungs-Cookie – muss nicht gespeichert werden."""
    return _hmac(browser_token, b"musichub-pkce")


def safe_return_to(value: str | None) -> str:
    """Nur relative Pfade innerhalb von MusicHub (Schutz vor Open Redirects)."""
    if not value or len(value) > 1000:
        return "/"
    if not value.startswith("/") or value.startswith("//") or "\\" in value:
        return "/"
    if any(ord(c) < 0x20 for c in value) or value.startswith("/api/"):
        return "/"
    return value


class SessionManager:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.secure = settings.session_cookie_secure_effective
        prefix = "__Host-" if self.secure else ""
        self.session_cookie = f"{prefix}musichub_session"
        self.csrf_cookie = f"{prefix}musichub_csrf"
        self.login_cookie = f"{'__Secure-' if self.secure else ''}musichub_login"
        self.idle = timedelta(minutes=settings.session_idle_minutes)
        self.max_age = timedelta(hours=settings.session_max_hours)
        self.allowed_origin = _origin(settings.public_url) if settings.public_url else None

    # --- Cookies -----------------------------------------------------------------

    def set_session_cookies(self, response: Response, token: str, expires_at: datetime) -> None:
        max_age = max(0, int((expires_at - _now()).total_seconds()))
        response.set_cookie(
            self.session_cookie,
            token,
            max_age=max_age,
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )
        response.set_cookie(
            self.csrf_cookie,
            csrf_token_for(token),
            max_age=max_age,
            path="/",
            secure=self.secure,
            httponly=False,
            samesite="strict",
        )

    def clear_session_cookies(self, response: Response) -> None:
        for name, httponly, samesite in (
            (self.session_cookie, True, "lax"),
            (self.csrf_cookie, False, "strict"),
        ):
            response.delete_cookie(
                name, path="/", secure=self.secure, httponly=httponly, samesite=samesite
            )

    def set_login_cookie(self, response: Response, token: str) -> None:
        response.set_cookie(
            self.login_cookie,
            token,
            max_age=int(LOGIN_TTL.total_seconds()),
            path="/api/auth",
            secure=self.secure,
            httponly=True,
            samesite="lax",
        )

    def clear_login_cookie(self, response: Response) -> None:
        response.delete_cookie(
            self.login_cookie, path="/api/auth", secure=self.secure, httponly=True, samesite="lax"
        )

    # --- Login-Zustand ---------------------------------------------------------

    def start_login(self, db: Session, return_to: str) -> tuple[str, str, str]:
        """Legt einen Login an. Gibt (state, nonce, browser_token) zurück."""
        now = _now()
        db.execute(delete(AuthLogin).where(AuthLogin.expires_at < now))
        state, nonce, browser = (secrets.token_urlsafe(32) for _ in range(3))
        db.add(
            AuthLogin(
                state_hash=token_hash(state),
                browser_hash=token_hash(browser),
                nonce=nonce,
                return_to=safe_return_to(return_to),
                created_at=now,
                expires_at=now + LOGIN_TTL,
            )
        )
        return state, nonce, browser

    def consume_login(self, db: Session, state: str, browser: str | None) -> AuthLogin | None:
        """Holt und löscht den Login (einmalig). ``None`` = unbekannt, abgelaufen oder
        aus einem anderen Browser (Login-CSRF)."""
        login = db.scalar(
            select(AuthLogin).where(AuthLogin.state_hash == token_hash(state)).with_for_update()
        )
        if login is None:
            return None
        db.delete(login)
        db.flush()
        if login.expires_at < _now():
            return None
        if not browser or not hmac.compare_digest(login.browser_hash, token_hash(browser)):
            log.warning("Login-Rückruf ohne passendes Bindungs-Cookie abgelehnt")
            return None
        return login

    # --- Sitzungen ---------------------------------------------------------------

    def create(
        self,
        db: Session,
        *,
        subject: str,
        display_name: str,
        email: str | None,
        groups: list[str],
        can_write: bool,
    ) -> tuple[str, UserSession]:
        now = _now()
        token = secrets.token_urlsafe(32)
        row = UserSession(
            token_hash=token_hash(token),
            subject=subject[:255],
            display_name=display_name[:255],
            email=email[:320] if email else None,
            groups=groups[:200],
            can_write=can_write,
            created_at=now,
            last_seen_at=now,
            expires_at=now + self.max_age,
        )
        db.add(row)
        return token, row

    def authenticate(self, db: Session, token: str) -> Principal:
        row = db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))
        if row is None:
            raise SessionExpiredError("Sitzung abgelaufen. Bitte erneut anmelden.")
        now = _now()
        if row.expires_at <= now or row.last_seen_at + self.idle <= now:
            db.delete(row)
            db.commit()
            raise SessionExpiredError("Sitzung abgelaufen. Bitte erneut anmelden.")
        if now - row.last_seen_at >= TOUCH_INTERVAL:
            row.last_seen_at = now
            db.commit()
        return Principal(
            subject=row.subject,
            display_name=row.display_name,
            groups=frozenset(row.groups or []),
            can_write=row.can_write,
            auth_method="session",
        )

    def lookup(self, db: Session, token: str) -> UserSession | None:
        return db.scalar(select(UserSession).where(UserSession.token_hash == token_hash(token)))

    def destroy(self, db: Session, token: str) -> None:
        db.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))

    def idle_expires_at(self, row: UserSession) -> datetime:
        return min(row.expires_at, row.last_seen_at + self.idle)

    # --- CSRF --------------------------------------------------------------------

    def check_csrf(self, request: Request, token: str) -> None:
        if request.method in SAFE_METHODS:
            return
        origin = request.headers.get("origin")
        if origin and self.allowed_origin and origin.rstrip("/") != self.allowed_origin:
            log.warning("Anfrage mit fremdem Origin abgelehnt")
            raise ForbiddenError("Anfrage von fremder Herkunft abgelehnt.", extra={"csrf": True})
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, csrf_token_for(token)):
            raise ForbiddenError("CSRF-Token fehlt oder ist ungültig.", extra={"csrf": True})


def purge_expired(db: Session, settings: Settings) -> int:
    """Abgelaufene Sitzungen und Logins löschen (läuft im Worker)."""
    now = _now()
    idle_cutoff = now - timedelta(minutes=settings.session_idle_minutes)
    result = db.execute(
        delete(UserSession).where(
            (UserSession.expires_at <= now) | (UserSession.last_seen_at <= idle_cutoff)
        )
    )
    db.execute(delete(AuthLogin).where(AuthLogin.expires_at < now))
    return int(getattr(result, "rowcount", 0) or 0)


def principal_claims(
    claims: dict[str, Any], groups_claim: str
) -> tuple[str, str, str | None, list[str]]:
    raw = claims.get(groups_claim) or []
    groups = [str(g) for g in raw] if isinstance(raw, list) else []
    name = claims.get("preferred_username") or claims.get("name") or claims["sub"]
    email = claims.get("email") if isinstance(claims.get("email"), str) else None
    return str(claims["sub"]), str(name), email, groups


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"
