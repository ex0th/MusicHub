"""Browser-Anmeldung über Authentik (OIDC Authorization Code Flow mit PKCE).

Liegt außerhalb von ``/api/v1``: Login und Rückruf sind Browser-Weiterleitungen, keine
API-Aufrufe. Ablauf und Sicherheitsmerkmale: ``docs/SECURITY.md``.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from musichub.api.schemas import ApiModel
from musichub.auth.oidc import OidcError, OidcProvider
from musichub.auth.sessions import SessionManager, pkce_verifier_for, principal_claims
from musichub.core.config import AuthMode, Settings
from musichub.core.errors import ServiceUnavailableError, UnauthorizedError
from musichub.core.ratelimit import RateLimiter, client_key
from musichub.db.session import get_db

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])

LOGIN_PAGE = "/login/"


class SessionUser(ApiModel):
    subject: str
    display_name: str
    email: str | None
    groups: list[str]
    can_write: bool


class SessionInfo(ApiModel):
    mode: Literal["dev", "oidc"]
    #: Browser-Anmeldung über Authentik ist eingerichtet
    login_enabled: bool
    authenticated: bool
    user: SessionUser | None = None
    #: absolutes Ende der Sitzung
    expires_at: datetime | None = None
    #: Ende bei weiterer Inaktivität
    idle_expires_at: datetime | None = None


class LogoutResponse(ApiModel):
    #: Ziel für den Browser: Abmeldung bei Authentik oder Login-Seite
    redirect: str


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _sessions(request: Request) -> SessionManager | None:
    sessions: SessionManager | None = getattr(request.app.state, "sessions", None)
    return sessions


def _provider(request: Request) -> OidcProvider | None:
    provider: OidcProvider | None = getattr(request.app.state, "oidc", None)
    return provider


def _limit(request: Request) -> None:
    limiter: RateLimiter = request.app.state.auth_limiter
    limiter.hit(client_key(request))


def _login_error(code: str) -> RedirectResponse:
    return RedirectResponse(f"{LOGIN_PAGE}?{urlencode({'error': code})}", status_code=303)


@router.get("/session", response_model=SessionInfo, summary="Anmeldestatus des Browsers")
def session_info(request: Request, db: Session = Depends(get_db)) -> SessionInfo:
    settings = _settings(request)
    mode: Literal["dev", "oidc"] = "dev" if settings.auth_mode is AuthMode.DEV else "oidc"
    info = SessionInfo(mode=mode, login_enabled=settings.oidc_login_enabled, authenticated=False)
    sessions = _sessions(request)
    token = request.cookies.get(sessions.session_cookie) if sessions else None
    if not sessions or not token:
        return info
    try:
        sessions.authenticate(db, token)
    except UnauthorizedError:
        return info  # abgelaufen: Cookies löscht die Login-Seite beim nächsten Login
    row = sessions.lookup(db, token)
    if row is None:
        return info
    info.authenticated = True
    info.user = SessionUser(
        subject=row.subject,
        display_name=row.display_name,
        email=row.email,
        groups=sorted(row.groups or []),
        can_write=row.can_write,
    )
    info.expires_at = row.expires_at
    info.idle_expires_at = sessions.idle_expires_at(row)
    return info


@router.get(
    "/login",
    status_code=302,
    summary="Anmeldung starten (Weiterleitung zu Authentik)",
    response_class=RedirectResponse,
)
def login(
    request: Request,
    db: Session = Depends(get_db),
    next: str | None = Query(default=None, max_length=1000, description="Rückkehrpfad"),
) -> RedirectResponse:
    settings = _settings(request)
    if settings.auth_mode is AuthMode.DEV:
        return RedirectResponse("/settings/", status_code=302)
    _limit(request)
    sessions, provider = _sessions(request), _provider(request)
    if not settings.oidc_login_enabled or sessions is None or provider is None:
        return _login_error("not_configured")
    state, nonce, browser = sessions.start_login(db, next or "/")
    try:
        url = provider.authorization_url(
            redirect_uri=settings.oidc_redirect_uri or "",
            state=state,
            nonce=nonce,
            code_verifier=pkce_verifier_for(browser),
            scopes=settings.oidc_scopes,
        )
    except ServiceUnavailableError:
        db.rollback()
        return _login_error("provider_unavailable")
    db.commit()
    response = RedirectResponse(url, status_code=302)
    response.headers["Cache-Control"] = "no-store"
    sessions.set_login_cookie(response, browser)
    return response


@router.get(
    "/callback",
    status_code=303,
    summary="Rückruf von Authentik (Code gegen Sitzung tauschen)",
    response_class=RedirectResponse,
)
def callback(
    request: Request,
    db: Session = Depends(get_db),
    code: str | None = Query(default=None, max_length=2048),
    state: str | None = Query(default=None, max_length=512),
    error: str | None = Query(default=None, max_length=128),
) -> RedirectResponse:
    settings = _settings(request)
    sessions, provider = _sessions(request), _provider(request)
    if not settings.oidc_login_enabled or sessions is None or provider is None:
        return _login_error("not_configured")
    _limit(request)

    def fail(code_: str) -> RedirectResponse:
        response = _login_error(code_)
        sessions.clear_login_cookie(response)
        return response

    if not state:
        return fail("invalid_state")
    browser = request.cookies.get(sessions.login_cookie)
    pending = sessions.consume_login(db, state, browser)
    db.commit()  # Login ist in jedem Fall verbraucht
    if pending is None:
        return fail("invalid_state")
    if error:
        log.info("Anmeldung vom Provider abgebrochen (%s)", error[:64])
        return fail("access_denied" if error == "access_denied" else "provider_error")
    if not code or browser is None:
        return fail("invalid_request")

    try:
        tokens = provider.exchange_code(
            code=code,
            redirect_uri=settings.oidc_redirect_uri or "",
            code_verifier=pkce_verifier_for(browser),
        )
        claims = provider.verify_id_token(tokens.id_token, nonce=pending.nonce)
    except OidcError as exc:
        log.warning("Anmeldung fehlgeschlagen: %s (%s)", exc.code, exc)
        return fail(exc.code)
    except ServiceUnavailableError:
        return fail("provider_unavailable")

    if settings.oidc_groups_claim not in claims and tokens.access_token:
        extra = provider.userinfo(tokens.access_token)
        if extra.get("sub") == claims["sub"]:
            claims = {**extra, **claims}
    subject, name, email, groups = principal_claims(claims, settings.oidc_groups_claim)
    if settings.oidc_required_group and settings.oidc_required_group not in groups:
        log.warning("Anmeldung ohne erforderliche Gruppe abgelehnt")
        return fail("forbidden")
    can_write = settings.oidc_write_group is None or settings.oidc_write_group in groups

    # Sitzungs-Fixierung vermeiden: eine vorhandene Sitzung dieses Browsers ersetzen
    old = request.cookies.get(sessions.session_cookie)
    if old:
        sessions.destroy(db, old)
    token, row = sessions.create(
        db, subject=subject, display_name=name, email=email, groups=groups, can_write=can_write
    )
    db.commit()
    log.info("Anmeldung erfolgreich", extra={"subject": subject})
    response = RedirectResponse(pending.return_to, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    sessions.clear_login_cookie(response)
    sessions.set_session_cookies(response, token, row.expires_at)
    return response


@router.post("/logout", response_model=LogoutResponse, summary="Abmelden")
def logout(request: Request, db: Session = Depends(get_db)) -> JSONResponse:
    settings = _settings(request)
    sessions, provider = _sessions(request), _provider(request)
    target = f"{LOGIN_PAGE}?logged_out=1"
    if sessions is None:
        return JSONResponse(LogoutResponse(redirect=target).model_dump())
    token = request.cookies.get(sessions.session_cookie)
    if token:
        sessions.check_csrf(request, token)
        sessions.destroy(db, token)
        db.commit()
        if settings.oidc_logout_at_provider and provider is not None:
            target = provider.end_session_url() or target
    response = JSONResponse(LogoutResponse(redirect=target).model_dump())
    sessions.clear_session_cookies(response)
    return response
