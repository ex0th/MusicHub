import json
import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from musichub.auth.verifiers import DevTokenVerifier, OidcVerifier
from musichub.core.config import Settings
from musichub.core.errors import UnauthorizedError

ISSUER = "https://auth.example.test/application/o/musichub"
AUDIENCE = "musichub"


# --- Konfiguration -------------------------------------------------------------


def test_dev_auth_forbidden_in_production() -> None:
    with pytest.raises(ValidationError, match="production"):
        Settings(app_env="production", auth_mode="dev", dev_auth_token="x" * 32)


def test_dev_auth_requires_long_token() -> None:
    with pytest.raises(ValidationError, match="DEV_AUTH_TOKEN"):
        Settings(app_env="development", auth_mode="dev", dev_auth_token="short")


def test_oidc_requires_issuer_and_audience() -> None:
    with pytest.raises(ValidationError, match="OIDC_ISSUER"):
        Settings(app_env="production", auth_mode="oidc")
    Settings(app_env="production", auth_mode="oidc", oidc_issuer=ISSUER, oidc_audience=AUDIENCE)


def test_dev_verifier() -> None:
    verifier = DevTokenVerifier("a" * 20, "silas")
    assert verifier.verify("a" * 20).can_write
    with pytest.raises(UnauthorizedError):
        verifier.verify("b" * 20)


# --- OIDC ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def rsa_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(rsa_key: rsa.RSAPrivateKey) -> OidcVerifier:
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(rsa_key.public_key()))
    jwk.update({"kid": "k1", "use": "sig", "alg": "RS256"})
    v = OidcVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks_url="https://unused.test/jwks",
        write_group="musichub-admins",
    )
    client = jwt.PyJWKClient("https://unused.test/jwks")
    client.fetch_data = lambda: {"keys": [jwk]}  # type: ignore[method-assign]
    v._client = client
    return v


def make_token(key: rsa.RSAPrivateKey, **overrides: Any) -> str:
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-1",
        "preferred_username": "silas",
        "groups": ["musichub-admins"],
        "exp": int(time.time()) + 300,
    }
    claims.update(overrides)
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


def test_oidc_valid_token(verifier: OidcVerifier, rsa_key: rsa.RSAPrivateKey) -> None:
    principal = verifier.verify(make_token(rsa_key))
    assert principal.subject == "user-1"
    assert principal.display_name == "silas"
    assert principal.can_write


def test_oidc_without_write_group_is_read_only(
    verifier: OidcVerifier, rsa_key: rsa.RSAPrivateKey
) -> None:
    assert not verifier.verify(make_token(rsa_key, groups=["users"])).can_write


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other-app"},
        {"iss": "https://evil.test"},
        {"exp": int(time.time()) - 3600},
    ],
)
def test_oidc_rejects_invalid_claims(
    verifier: OidcVerifier, rsa_key: rsa.RSAPrivateKey, overrides: dict[str, Any]
) -> None:
    with pytest.raises(UnauthorizedError):
        verifier.verify(make_token(rsa_key, **overrides))


def test_oidc_rejects_foreign_signature(verifier: OidcVerifier) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(UnauthorizedError):
        verifier.verify(make_token(other))


def test_oidc_rejects_garbage(verifier: OidcVerifier) -> None:
    with pytest.raises(UnauthorizedError):
        verifier.verify("not-a-jwt")


def test_empty_env_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from musichub.connectors.platforms import YouTubeConnector

    monkeypatch.setenv("MUSICHUB_YOUTUBE_API_KEY", "")
    monkeypatch.setenv("MUSICHUB_OIDC_WRITE_GROUP", "")
    settings = Settings()
    assert settings.youtube_api_key is None
    assert settings.oidc_write_group is None
    assert not YouTubeConnector(settings).is_configured()
