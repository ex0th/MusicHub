from __future__ import annotations

import hmac
import logging
from typing import Any, Protocol

import httpx

from musichub.auth.oidc import OidcProvider
from musichub.auth.principal import Principal
from musichub.auth.sessions import principal_claims
from musichub.core.config import AuthMode, Settings
from musichub.core.errors import ForbiddenError, UnauthorizedError

log = logging.getLogger(__name__)


class TokenVerifier(Protocol):
    method: str

    def verify(self, token: str) -> Principal: ...


class DevTokenVerifier:
    """Vergleicht das Bearer-Token zeitkonstant mit MUSICHUB_DEV_AUTH_TOKEN."""

    method = "dev"

    def __init__(self, token: str, user: str) -> None:
        self._token = token.encode()
        self._user = user

    def verify(self, token: str) -> Principal:
        if not hmac.compare_digest(token.encode(), self._token):
            raise UnauthorizedError("Ungültiges Token.")
        return Principal(
            subject=f"dev:{self._user}",
            display_name=self._user,
            can_write=True,
            auth_method=self.method,
        )


class OidcVerifier:
    """Validiert JWT-Access-Tokens (Bearer) über das JWKS des OIDC-Providers.

    Für Skripte und API-Clients. Browser melden sich über den Authorization Code Flow an
    und nutzen eine serverseitige Sitzung (``auth/sessions.py``).
    """

    method = "oidc"

    def __init__(
        self,
        provider: OidcProvider,
        *,
        write_group: str | None = None,
        groups_claim: str = "groups",
        required_group: str | None = None,
    ) -> None:
        self._provider = provider
        self._write_group = write_group
        self._groups_claim = groups_claim
        self._required_group = required_group

    def verify(self, token: str) -> Principal:
        return self.principal_from_claims(self._provider.verify_access_token(token))

    def principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        subject, name, _email, groups = principal_claims(claims, self._groups_claim)
        group_set = frozenset(groups)
        if self._required_group and self._required_group not in group_set:
            raise ForbiddenError("Kein Zugriff auf MusicHub (Gruppe fehlt).")
        return Principal(
            subject=subject,
            display_name=name,
            groups=group_set,
            can_write=self._write_group is None or self._write_group in group_set,
            auth_method=self.method,
        )


def build_verifier(settings: Settings, provider: OidcProvider | None = None) -> TokenVerifier:
    if settings.auth_mode is AuthMode.DEV:
        assert settings.dev_auth_token is not None  # durch Settings-Validierung garantiert
        log.warning(
            "Entwicklungs-Authentifizierung aktiv (MUSICHUB_AUTH_MODE=dev). "
            "Nicht für öffentlich erreichbare Instanzen verwenden."
        )
        return DevTokenVerifier(settings.dev_auth_token.get_secret_value(), settings.dev_auth_user)
    assert provider is not None
    return OidcVerifier(
        provider,
        write_group=settings.oidc_write_group,
        groups_claim=settings.oidc_groups_claim,
        required_group=settings.oidc_required_group,
    )


def build_provider(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> OidcProvider | None:
    if settings.auth_mode is not AuthMode.OIDC:
        return None
    assert settings.oidc_issuer and settings.oidc_expected_audience
    secret = settings.oidc_client_secret
    return OidcProvider(
        issuer=settings.oidc_issuer,
        client_id=settings.oidc_client_id,
        client_secret=secret.get_secret_value() if secret else None,
        audience=settings.oidc_expected_audience,
        jwks_url=settings.oidc_jwks_url,
        transport=transport,
    )
