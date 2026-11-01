from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import APIKeyCookie, HTTPAuthorizationCredentials, HTTPBearer

from musichub.auth.principal import Principal
from musichub.auth.sessions import SessionManager
from musichub.auth.verifiers import TokenVerifier
from musichub.core.errors import ForbiddenError, UnauthorizedError
from musichub.db.session import get_sessionmaker

_bearer = HTTPBearer(
    auto_error=False, description="Bearer-Token (dev-Token oder OIDC-Access-Token)"
)
# Nur für die OpenAPI-Beschreibung; gelesen wird das Cookie über den SessionManager,
# weil der Name vom Secure-Flag abhängt (__Host-musichub_session).
_cookie = APIKeyCookie(
    name="__Host-musichub_session",
    auto_error=False,
    description="Sitzungs-Cookie nach Anmeldung über /api/auth/login",
)


def get_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.verifier
    return verifier


def get_sessions(request: Request) -> SessionManager | None:
    sessions: SessionManager | None = getattr(request.app.state, "sessions", None)
    return sessions


def current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    _cookie_doc: str | None = Depends(_cookie),
    verifier: TokenVerifier = Depends(get_verifier),
) -> Principal:
    """Angemeldeter Aufrufer: Bearer-Token (API-Clients, dev) oder Sitzungs-Cookie (Browser).

    Bei Sitzungs-Cookies prüft diese Dependency für verändernde Methoden zusätzlich den
    CSRF-Token – jede schreibende Route hängt über ``require_write`` davon ab.
    """
    if credentials is not None and credentials.scheme.lower() == "bearer":
        return verifier.verify(credentials.credentials)
    sessions = get_sessions(request)
    if sessions is not None:
        token = request.cookies.get(sessions.session_cookie)
        if token:
            with get_sessionmaker()() as db:
                principal = sessions.authenticate(db, token)
            sessions.check_csrf(request, token)
            return principal
    raise UnauthorizedError("Authentifizierung erforderlich.")


def require_write(principal: Principal = Depends(current_principal)) -> Principal:
    """Pflicht-Dependency für jeden Endpunkt, der Daten verändert."""
    if not principal.can_write:
        raise ForbiddenError("Keine Schreibberechtigung.")
    return principal
