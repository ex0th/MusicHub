from __future__ import annotations

import hmac
import logging
import threading
from typing import Any, Protocol

import httpx
import jwt

from musichub.auth.principal import Principal
from musichub.core.config import AuthMode, Settings
from musichub.core.errors import ServiceUnavailableError, UnauthorizedError

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
    """Validiert JWT-Access-Tokens gegen das JWKS des OIDC-Providers.

    Die Schlüssel werden von PyJWKClient gecacht und bei unbekannter ``kid``
    automatisch neu geladen (Key-Rotation).
    """

    method = "oidc"
    _ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256")

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks_url: str | None = None,
        write_group: str | None = None,
        groups_claim: str = "groups",
    ) -> None:
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._jwks_url = jwks_url
        self._write_group = write_group
        self._groups_claim = groups_claim
        self._client: jwt.PyJWKClient | None = None
        self._lock = threading.Lock()

    def _discover_jwks_url(self) -> str:
        url = f"{self._issuer}/.well-known/openid-configuration"
        try:
            resp = httpx.get(url, timeout=5.0)
            resp.raise_for_status()
            return str(resp.json()["jwks_uri"])
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            log.error("OIDC-Discovery fehlgeschlagen (%s): %s", url, exc)
            raise ServiceUnavailableError("Identity-Provider nicht erreichbar.") from exc

    def _jwk_client(self) -> jwt.PyJWKClient:
        with self._lock:
            if self._client is None:
                url = self._jwks_url or self._discover_jwks_url()
                self._client = jwt.PyJWKClient(url, cache_keys=True, lifespan=3600)
            return self._client

    def verify(self, token: str) -> Principal:
        try:
            signing_key = self._jwk_client().get_signing_key_from_jwt(token)
        except jwt.PyJWKClientConnectionError as exc:
            raise ServiceUnavailableError("Identity-Provider nicht erreichbar.") from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Ungültiges Token.") from exc

        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self._ALGORITHMS),
                audience=self._audience,
                issuer=self._issuer,
                options={"require": ["exp", "iss", "sub"]},
                leeway=30,
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("Token abgelaufen.") from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Ungültiges Token.") from exc

        return self.principal_from_claims(claims)

    def principal_from_claims(self, claims: dict[str, Any]) -> Principal:
        raw_groups = claims.get(self._groups_claim) or []
        groups = (
            frozenset(str(g) for g in raw_groups) if isinstance(raw_groups, list) else frozenset()
        )
        can_write = self._write_group is None or self._write_group in groups
        return Principal(
            subject=str(claims["sub"]),
            display_name=str(
                claims.get("preferred_username") or claims.get("name") or claims["sub"]
            ),
            groups=groups,
            can_write=can_write,
            auth_method=self.method,
        )


def build_verifier(settings: Settings) -> TokenVerifier:
    if settings.auth_mode is AuthMode.DEV:
        assert settings.dev_auth_token is not None  # durch Settings-Validierung garantiert
        log.warning(
            "Entwicklungs-Authentifizierung aktiv (MUSICHUB_AUTH_MODE=dev). "
            "Nicht für öffentlich erreichbare Instanzen verwenden."
        )
        return DevTokenVerifier(settings.dev_auth_token.get_secret_value(), settings.dev_auth_user)
    assert settings.oidc_issuer and settings.oidc_audience
    return OidcVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        jwks_url=settings.oidc_jwks_url,
        write_group=settings.oidc_write_group,
        groups_claim=settings.oidc_groups_claim,
    )
