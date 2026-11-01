"""Nachgebildeter OIDC-Provider (wie Authentik) als ``httpx.MockTransport``.

Bildet Discovery, JWKS, Token-Endpunkt (mit PKCE- und Client-Prüfung), Userinfo und
End-Session nach. ``authorize()`` entspricht der Zustimmung des Nutzers im Browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

ISSUER = "https://auth.example.test/application/o/musichub/"
CLIENT_ID = "musichub-client"
CLIENT_SECRET = "musichub-secret-0123456789"
PUBLIC_URL = "https://music.example.test"
REDIRECT_URI = f"{PUBLIC_URL}/api/auth/callback"


@dataclass
class _Grant:
    claims: dict[str, Any]
    nonce: str
    challenge: str
    redirect_uri: str


@dataclass
class FakeIdP:
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    kid: str = "k1"
    grants: dict[str, _Grant] = field(default_factory=dict)
    #: Steuerung für Fehlerfälle
    id_token_overrides: dict[str, Any] = field(default_factory=dict)
    include_groups_in_id_token: bool = True
    token_status: int = 200
    down: bool = False
    calls: list[str] = field(default_factory=list)
    userinfo_claims: dict[str, Any] = field(default_factory=dict)

    # --- Browser-Seite ---------------------------------------------------------------

    def authorize(self, url: str, **claims: Any) -> str:
        """Nutzer meldet sich an und stimmt zu → Autorisierungscode."""
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        assert params["response_type"] == "code"
        assert params["client_id"] == CLIENT_ID
        assert params["code_challenge_method"] == "S256"
        assert "openid" in params["scope"].split()
        code = secrets.token_urlsafe(16)
        base = {
            "sub": "user-1",
            "preferred_username": "silas",
            "email": "silas@example.test",
            "groups": ["musichub-admins"],
        }
        base.update(claims)
        self.grants[code] = _Grant(
            claims=base,
            nonce=params["nonce"],
            challenge=params["code_challenge"],
            redirect_uri=params["redirect_uri"],
        )
        return code

    # --- Tokens ------------------------------------------------------------------------

    def sign(self, claims: dict[str, Any], *, key: rsa.RSAPrivateKey | None = None) -> str:
        return jwt.encode(claims, key or self.key, algorithm="RS256", headers={"kid": self.kid})

    def access_token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "sub": "user-1",
            "preferred_username": "silas",
            "groups": ["musichub-admins"],
            "iat": now,
            "exp": now + 300,
        }
        claims.update(overrides)
        return self.sign(claims)

    def _jwks(self) -> dict[str, Any]:
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    # --- HTTP ----------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(f"{request.method} {path}")
        if self.down:
            raise httpx.ConnectError("down", request=request)
        base = "/application/o/musichub"
        if path == f"{base}/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": "https://auth.example.test/application/o/authorize/",
                    "token_endpoint": "https://auth.example.test/application/o/token/",
                    "userinfo_endpoint": "https://auth.example.test/application/o/userinfo/",
                    "end_session_endpoint": f"https://auth.example.test{base}/end-session/",
                    "jwks_uri": f"https://auth.example.test{base}/jwks/",
                },
            )
        if path == f"{base}/jwks/":
            return httpx.Response(200, json=self._jwks())
        if path == "/application/o/token/" and request.method == "POST":
            return self._token(request)
        if path == "/application/o/userinfo/":
            return httpx.Response(200, json=self.userinfo_claims)
        return httpx.Response(404, json={"error": "not_found"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        if self.token_status != 200:
            return httpx.Response(self.token_status, json={"error": "invalid_grant"})
        auth = request.headers.get("authorization", "")
        expected = base64.b64encode(f"{CLIENT_ID}:{CLIENT_SECRET}".encode()).decode()
        if auth != f"Basic {expected}":
            return httpx.Response(401, json={"error": "invalid_client"})
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        grant = self.grants.pop(form.get("code", ""), None)
        if grant is None or form.get("grant_type") != "authorization_code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("redirect_uri") != grant.redirect_uri:
            return httpx.Response(400, json={"error": "invalid_grant"})
        digest = hashlib.sha256(form.get("code_verifier", "").encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant.challenge:
            return httpx.Response(400, json={"error": "invalid_grant", "reason": "pkce"})
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "aud": CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": grant.nonce,
            **grant.claims,
        }
        if not self.include_groups_in_id_token:
            claims.pop("groups", None)
            self.userinfo_claims = {"sub": claims["sub"], "groups": grant.claims["groups"]}
        claims.update(self.id_token_overrides)
        return httpx.Response(
            200,
            json={
                "access_token": self.access_token(sub=claims["sub"]),
                "token_type": "Bearer",
                "expires_in": 300,
                "id_token": self.sign(claims),
            },
        )
