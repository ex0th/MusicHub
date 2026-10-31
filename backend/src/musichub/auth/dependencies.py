from __future__ import annotations

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from musichub.auth.principal import Principal
from musichub.auth.verifiers import TokenVerifier
from musichub.core.errors import ForbiddenError, UnauthorizedError

_bearer = HTTPBearer(
    auto_error=False, description="Bearer-Token (dev-Token oder OIDC-Access-Token)"
)


def get_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.verifier
    return verifier


def current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    verifier: TokenVerifier = Depends(get_verifier),
) -> Principal:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise UnauthorizedError("Authentifizierung erforderlich.")
    return verifier.verify(credentials.credentials)


def require_write(principal: Principal = Depends(current_principal)) -> Principal:
    """Pflicht-Dependency für jeden Endpunkt, der Daten verändert."""
    if not principal.can_write:
        raise ForbiddenError("Keine Schreibberechtigung.")
    return principal
