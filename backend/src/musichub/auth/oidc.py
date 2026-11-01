"""OpenID-Connect-Anbindung (Authentik): Discovery, Schlüssel, Code-Tausch, Token-Prüfung.

Ein ``OidcProvider`` je Anwendung. Er kapselt alle Netzwerkzugriffe auf den
Identity-Provider (über einen injizierbaren ``httpx.Client``) und prüft:

* ID-Tokens aus dem Authorization Code Flow (Signatur, ``iss``, ``aud``, ``exp``,
  ``iat``, ``nonce``, ``azp``),
* Bearer-Access-Tokens für API-Clients (Signatur, ``iss``, ``aud``, ``exp``).

Nur asymmetrische Signaturen werden akzeptiert. In Authentik muss am Provider deshalb ein
Signaturschlüssel ausgewählt sein (sonst signiert Authentik mit HS256 und dem Client-Secret).
"""

from __future__ import annotations

import base64
import hashlib
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from musichub.core.errors import ServiceUnavailableError, UnauthorizedError

log = logging.getLogger(__name__)

ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512", "PS256", "PS384", "PS512")
_DISCOVERY_TTL = 3600.0
_JWKS_TTL = 3600.0
#: unbekannte ``kid`` lädt die Schlüssel höchstens so oft neu (Schutz vor Schlüssel-Flut)
_JWKS_MIN_REFRESH = 60.0
_LEEWAY = 30


class OidcError(Exception):
    """Fehler im Login-Ablauf; ``code`` landet als Fehlerkennung in der Weiterleitung."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class TokenSet:
    id_token: str
    access_token: str | None


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class OidcProvider:
    def __init__(
        self,
        *,
        issuer: str,
        client_id: str | None,
        client_secret: str | None,
        audience: str,
        jwks_url: str | None = None,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        # Authentik nennt den Issuer mit abschließendem "/". Das "iss" im Token muss exakt
        # dem Discovery-Dokument entsprechen – wir vergleichen deshalb ohne/mit "/".
        self.issuer = issuer
        self.client_id = client_id
        self._client_secret = client_secret
        self.audience = audience
        self._jwks_url = jwks_url
        self._http = httpx.Client(timeout=timeout, transport=transport, follow_redirects=False)
        self._lock = threading.Lock()
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._jwks: jwt.PyJWKSet | None = None
        self._jwks_at = 0.0

    # --- Discovery und Schlüssel ------------------------------------------------

    def _get_json(self, url: str, what: str) -> dict[str, Any]:
        try:
            resp = self._http.get(url, headers={"Accept": "application/json"})
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.error("OIDC: %s nicht abrufbar (%s): %s", what, url, exc.__class__.__name__)
            raise ServiceUnavailableError("Identity-Provider nicht erreichbar.") from exc
        if not isinstance(data, dict):
            raise ServiceUnavailableError("Identity-Provider liefert eine ungültige Antwort.")
        return data

    def discovery(self) -> dict[str, Any]:
        with self._lock:
            fresh = self._discovery and time.monotonic() - self._discovery_at < _DISCOVERY_TTL
            if fresh:
                assert self._discovery is not None
                return self._discovery
        url = f"{self.issuer.rstrip('/')}/.well-known/openid-configuration"
        data = self._get_json(url, "Discovery")
        for key in ("issuer", "authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not isinstance(data.get(key), str):
                raise ServiceUnavailableError(f"Discovery-Dokument ohne {key}.")
        if data["issuer"].rstrip("/") != self.issuer.rstrip("/"):
            log.error("OIDC: Issuer passt nicht (%s ≠ %s)", data["issuer"], self.issuer)
            raise ServiceUnavailableError("OIDC-Issuer passt nicht zur Konfiguration.")
        with self._lock:
            self._discovery, self._discovery_at = data, time.monotonic()
        return data

    @property
    def token_issuer(self) -> str:
        """Der Issuer genau so, wie er in Tokens steht (aus der Discovery, falls verfügbar)."""
        return str(self._discovery["issuer"]) if self._discovery else self.issuer

    def _key_set(self, *, force: bool = False) -> jwt.PyJWKSet:
        with self._lock:
            age = time.monotonic() - self._jwks_at
            if self._jwks is not None and age < _JWKS_TTL and not force:
                return self._jwks
            if self._jwks is not None and force and age < _JWKS_MIN_REFRESH:
                return self._jwks
        url = self._jwks_url or str(self.discovery()["jwks_uri"])
        data = self._get_json(url, "JWKS")
        try:
            key_set = jwt.PyJWKSet.from_dict(data)
        except jwt.PyJWTError as exc:
            raise ServiceUnavailableError("JWKS des Identity-Providers ungültig.") from exc
        with self._lock:
            self._jwks, self._jwks_at = key_set, time.monotonic()
        return key_set

    def _signing_key(self, token: str) -> Any:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Ungültiges Token.") from exc
        if header.get("alg") not in ALGORITHMS:
            raise UnauthorizedError("Token-Signaturverfahren nicht erlaubt.")
        kid = header.get("kid")
        for force in (False, True):
            keys = self._key_set(force=force).keys
            match = [k for k in keys if kid is None or k.key_id == kid]
            if len(match) == 1 or (kid is None and match):
                return match[0].key
        raise UnauthorizedError("Unbekannter Signaturschlüssel.")

    def _decode(self, token: str, *, audience: str, require: list[str]) -> dict[str, Any]:
        key = self._signing_key(token)
        issuers = sorted({self.token_issuer, self.issuer})
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=list(ALGORITHMS),
                audience=audience,
                issuer=issuers,
                options={"require": require},
                leeway=_LEEWAY,
            )
        except jwt.ExpiredSignatureError as exc:
            raise UnauthorizedError("Token abgelaufen.") from exc
        except jwt.PyJWTError as exc:
            raise UnauthorizedError("Ungültiges Token.") from exc
        return claims

    # --- Bearer-Tokens (API-Clients) -----------------------------------------------

    def verify_access_token(self, token: str) -> dict[str, Any]:
        return self._decode(token, audience=self.audience, require=["exp", "iss", "sub"])

    # --- Authorization Code Flow -----------------------------------------------------

    def authorization_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_verifier: str, scopes: str
    ) -> str:
        assert self.client_id
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "scope": scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{self.discovery()['authorization_endpoint']}?{urlencode(params)}"

    def exchange_code(self, *, code: str, redirect_uri: str, code_verifier: str) -> TokenSet:
        assert self.client_id and self._client_secret
        endpoint = str(self.discovery()["token_endpoint"])
        try:
            resp = self._http.post(
                endpoint,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                },
                auth=(self.client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            log.error("OIDC: Token-Endpunkt nicht erreichbar: %s", exc.__class__.__name__)
            raise OidcError("provider_unavailable", "Identity-Provider nicht erreichbar.") from exc
        if resp.status_code != 200:
            # Fehlercode des Providers loggen, nie den Body mit möglichen Token-Teilen
            try:
                err = str(resp.json().get("error", "unknown"))[:64]
            except ValueError:
                err = "unknown"
            log.warning("OIDC: Code-Tausch abgelehnt (HTTP %s, %s)", resp.status_code, err)
            raise OidcError("token_exchange_failed", "Anmeldung vom Provider abgelehnt.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise OidcError("token_exchange_failed", "Ungültige Antwort des Providers.") from exc
        id_token = data.get("id_token")
        if not isinstance(id_token, str):
            raise OidcError("token_exchange_failed", "Provider lieferte kein ID-Token.")
        access = data.get("access_token")
        return TokenSet(id_token=id_token, access_token=access if isinstance(access, str) else None)

    def verify_id_token(self, id_token: str, *, nonce: str) -> dict[str, Any]:
        assert self.client_id
        try:
            claims = self._decode(
                id_token, audience=self.client_id, require=["exp", "iat", "iss", "sub", "aud"]
            )
        except UnauthorizedError as exc:
            raise OidcError("invalid_id_token", exc.detail) from exc
        if claims.get("nonce") != nonce:
            raise OidcError("invalid_id_token", "Nonce passt nicht.")
        aud = claims.get("aud")
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != self.client_id:
            raise OidcError("invalid_id_token", "azp passt nicht.")
        return claims

    def userinfo(self, access_token: str) -> dict[str, Any]:
        endpoint = self.discovery().get("userinfo_endpoint")
        if not isinstance(endpoint, str):
            return {}
        try:
            resp = self._http.get(
                endpoint,
                headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            )
            if resp.status_code != 200:
                log.warning("OIDC: Userinfo HTTP %s", resp.status_code)
                return {}
            data = resp.json()
        except (httpx.HTTPError, ValueError):
            log.warning("OIDC: Userinfo nicht abrufbar")
            return {}
        return data if isinstance(data, dict) else {}

    def end_session_url(self) -> str | None:
        try:
            url = self.discovery().get("end_session_endpoint")
        except ServiceUnavailableError:
            return None
        return url if isinstance(url, str) else None

    def close(self) -> None:
        self._http.close()
