import time
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import ValidationError

from fake_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, PUBLIC_URL, FakeIdP
from musichub.auth.oidc import OidcProvider
from musichub.auth.verifiers import DevTokenVerifier, OidcVerifier
from musichub.core.config import Settings
from musichub.core.errors import ForbiddenError, ServiceUnavailableError, UnauthorizedError

AUDIENCE = CLIENT_ID


# --- Konfiguration -------------------------------------------------------------


def test_dev_auth_forbidden_in_production() -> None:
    with pytest.raises(ValidationError, match="production"):
        Settings(app_env="production", auth_mode="dev", dev_auth_token="x" * 32)


def test_dev_auth_requires_long_token() -> None:
    with pytest.raises(ValidationError, match="DEV_AUTH_TOKEN"):
        Settings(app_env="development", auth_mode="dev", dev_auth_token="short")


def test_oidc_requires_issuer_and_client() -> None:
    with pytest.raises(ValidationError, match="OIDC_ISSUER"):
        Settings(app_env="development", auth_mode="oidc")
    Settings(app_env="development", auth_mode="oidc", oidc_issuer=ISSUER, oidc_audience=AUDIENCE)


def _prod(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "production",
        "auth_mode": "oidc",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "public_url": PUBLIC_URL,
    }
    values.update(overrides)
    return Settings(**values)


def test_production_config_complete() -> None:
    settings = _prod()
    assert settings.oidc_login_enabled
    assert settings.session_cookie_secure_effective
    assert not settings.api_docs_enabled
    assert settings.oidc_redirect_uri == f"{PUBLIC_URL}/api/auth/callback"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"oidc_client_secret": None}, "CLIENT_SECRET"),
        ({"public_url": None}, "PUBLIC_URL"),
        ({"public_url": "http://music.example.test"}, "https://"),
        ({"session_cookie_secure": False}, "COOKIE_SECURE"),
        ({"cors_origins": ["*"]}, "CORS"),
        ({"public_url": "https://music.example.test/sub/path"}, "ohne Pfad"),
    ],
)
def test_production_rejects_unsafe_config(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _prod(**overrides)


def test_dev_verifier() -> None:
    verifier = DevTokenVerifier("a" * 20, "silas")
    assert verifier.verify("a" * 20).can_write
    with pytest.raises(UnauthorizedError):
        verifier.verify("b" * 20)


# --- OIDC (Bearer-Tokens) ----------------------------------------------------------


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


@pytest.fixture
def verifier(idp: FakeIdP) -> OidcVerifier:
    provider = OidcProvider(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=None,
        audience=CLIENT_ID,
        transport=idp.transport(),
    )
    return OidcVerifier(provider, write_group="musichub-admins")


def test_oidc_valid_token(verifier: OidcVerifier, idp: FakeIdP) -> None:
    principal = verifier.verify(idp.access_token())
    assert principal.subject == "user-1"
    assert principal.display_name == "silas"
    assert principal.can_write


def test_oidc_without_write_group_is_read_only(verifier: OidcVerifier, idp: FakeIdP) -> None:
    assert not verifier.verify(idp.access_token(groups=["users"])).can_write


def test_oidc_required_group(idp: FakeIdP) -> None:
    provider = OidcProvider(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=None,
        audience=CLIENT_ID,
        transport=idp.transport(),
    )
    v = OidcVerifier(provider, required_group="musichub-users")
    with pytest.raises(ForbiddenError):
        v.verify(idp.access_token(groups=["other"]))
    assert v.verify(idp.access_token(groups=["musichub-users"])).subject == "user-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other-app"},
        {"iss": "https://evil.test"},
        {"exp": int(time.time()) - 3600},
    ],
)
def test_oidc_rejects_invalid_claims(
    verifier: OidcVerifier, idp: FakeIdP, overrides: dict[str, Any]
) -> None:
    with pytest.raises(UnauthorizedError):
        verifier.verify(idp.access_token(**overrides))


def test_oidc_rejects_foreign_signature(verifier: OidcVerifier, idp: FakeIdP) -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = idp.sign(
        {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "exp": time.time() + 60}, key=other
    )
    with pytest.raises(UnauthorizedError):
        verifier.verify(token)


def test_oidc_rejects_symmetric_and_none_algorithms(verifier: OidcVerifier) -> None:
    claims = {"iss": ISSUER, "aud": CLIENT_ID, "sub": "x", "exp": time.time() + 60}
    hs = jwt.encode(claims, "secret-secret-secret-secret-1234", algorithm="HS256")
    none = jwt.encode(claims, None, algorithm="none")
    for token in (hs, none):
        with pytest.raises(UnauthorizedError):
            verifier.verify(token)


def test_oidc_rejects_garbage(verifier: OidcVerifier) -> None:
    with pytest.raises(UnauthorizedError):
        verifier.verify("not-a-jwt")


def test_oidc_provider_down_is_503(idp: FakeIdP, verifier: OidcVerifier) -> None:
    idp.down = True
    with pytest.raises(ServiceUnavailableError):
        verifier.verify(idp.access_token())


def test_empty_env_values_count_as_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    from musichub.connectors.platforms import YouTubeConnector

    monkeypatch.setenv("MUSICHUB_YOUTUBE_CLIENT_ID", "")
    monkeypatch.setenv("MUSICHUB_OIDC_WRITE_GROUP", "")
    settings = Settings()
    assert settings.youtube_client_id is None
    assert settings.oidc_write_group is None
    assert not YouTubeConnector(settings).is_configured()
