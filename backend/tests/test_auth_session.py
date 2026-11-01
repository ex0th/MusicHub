"""Browser-Anmeldung: Authorization Code Flow, Sitzung, CSRF, Ablauf, Abmeldung."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fake_idp import CLIENT_ID, CLIENT_SECRET, ISSUER, PUBLIC_URL, FakeIdP
from musichub.auth.sessions import csrf_token_for, purge_expired, safe_return_to
from musichub.core.config import Settings, get_settings
from musichub.main import create_app
from musichub.models import AuthLogin, UserSession

pytestmark = pytest.mark.db

SESSION = "__Host-musichub_session"
CSRF = "__Host-musichub_csrf"
LOGIN = "__Secure-musichub_login"


def oidc_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "auth_mode": "oidc",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_write_group": "musichub-admins",
        "public_url": PUBLIC_URL,
        "database_url": get_settings().database_url,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture
def idp() -> FakeIdP:
    return FakeIdP()


ClientFactory = Callable[..., TestClient]


@pytest.fixture
def make_client(idp: FakeIdP, db: Session) -> Iterator[ClientFactory]:
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        app = create_app(oidc_settings(**overrides), oidc_transport=idp.transport())
        client = TestClient(app, base_url=PUBLIC_URL, follow_redirects=False)
        client.__enter__()
        clients.append(client)
        return client

    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    return make_client()


def start_login(client: TestClient, next_path: str = "/") -> tuple[str, str]:
    resp = client.get("/api/auth/login", params={"next": next_path})
    assert resp.status_code == 302, resp.text
    location = resp.headers["location"]
    params = parse_qs(urlsplit(location).query)
    return location, params["state"][0]


def login(client: TestClient, idp: FakeIdP, next_path: str = "/", **claims: Any) -> httpx.Response:
    location, state = start_login(client, next_path)
    code = idp.authorize(location, **claims)
    return client.get("/api/auth/callback", params={"code": code, "state": state})


def csrf(client: TestClient) -> dict[str, str]:
    return {"X-CSRF-Token": client.cookies[CSRF]}


# --- Login ------------------------------------------------------------------------------


def test_login_redirects_to_provider_with_pkce(client: TestClient, db: Session) -> None:
    location, state = start_login(client, "/library/")
    url = urlsplit(location)
    params = {k: v[0] for k, v in parse_qs(url.query).items()}
    assert url.netloc == "auth.example.test"
    assert params["redirect_uri"] == f"{PUBLIC_URL}/api/auth/callback"
    assert params["code_challenge_method"] == "S256"
    assert len(params["code_challenge"]) == 43
    assert params["nonce"] and params["state"] == state
    assert params["scope"] == "openid profile email"
    assert LOGIN in client.cookies
    pending = db.scalar(select(AuthLogin))
    assert pending is not None and pending.return_to == "/library/"
    assert state not in pending.state_hash  # nur Hash gespeichert


def test_full_login_creates_server_side_session(
    client: TestClient, idp: FakeIdP, db: Session
) -> None:
    resp = login(client, idp, "/library/?q=abc")
    assert resp.status_code == 303
    assert resp.headers["location"] == "/library/?q=abc"
    assert resp.headers["cache-control"] == "no-store"

    cookies = resp.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith(f"{SESSION}="))
    assert "HttpOnly" in session_cookie and "Secure" in session_cookie
    assert "samesite=lax" in session_cookie.lower() and "Path=/" in session_cookie
    csrf_cookie = next(c for c in cookies if c.startswith(f"{CSRF}="))
    assert "HttpOnly" not in csrf_cookie and "samesite=strict" in csrf_cookie.lower()

    token = client.cookies[SESSION]
    row = db.scalar(select(UserSession))
    assert row is not None and row.subject == "user-1" and row.can_write
    assert token not in row.token_hash
    assert client.cookies[CSRF] == csrf_token_for(token)

    info = client.get("/api/auth/session").json()
    assert info["mode"] == "oidc" and info["authenticated"]
    assert info["user"]["display_name"] == "silas"
    assert info["user"]["email"] == "silas@example.test"
    me = client.get("/api/v1/me").json()
    assert me["auth_method"] == "session" and me["can_write"]


def test_protected_api_without_session(client: TestClient) -> None:
    resp = client.get("/api/v1/tracks")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"
    info = client.get("/api/auth/session").json()
    assert info == {
        "mode": "oidc",
        "login_enabled": True,
        "authenticated": False,
        "user": None,
        "expires_at": None,
        "idle_expires_at": None,
    }


def test_api_responses_are_not_cached(client: TestClient, idp: FakeIdP) -> None:
    login(client, idp)
    resp = client.get("/api/v1/tracks")
    assert resp.headers["cache-control"] == "no-store"
    assert resp.headers["x-content-type-options"] == "nosniff"


# --- CSRF und Rechte ------------------------------------------------------------------


def test_writes_require_csrf_token(client: TestClient, idp: FakeIdP) -> None:
    login(client, idp)
    body = {"name": "Rock"}
    missing = client.post("/api/v1/tags", json=body)
    assert missing.status_code == 403 and missing.json()["csrf"] is True
    wrong = client.post("/api/v1/tags", json=body, headers={"X-CSRF-Token": "x" * 43})
    assert wrong.status_code == 403
    ok = client.post("/api/v1/tags", json=body, headers=csrf(client))
    assert ok.status_code == 201, ok.text


def test_foreign_origin_is_rejected(client: TestClient, idp: FakeIdP) -> None:
    login(client, idp)
    resp = client.post(
        "/api/v1/tags",
        json={"name": "Jazz"},
        headers={**csrf(client), "Origin": "https://evil.example"},
    )
    assert resp.status_code == 403
    same = client.post(
        "/api/v1/tags", json={"name": "Jazz"}, headers={**csrf(client), "Origin": PUBLIC_URL}
    )
    assert same.status_code == 201


def test_user_without_write_group_is_read_only(client: TestClient, idp: FakeIdP) -> None:
    login(client, idp, groups=["musichub-users"])
    assert client.get("/api/v1/tracks").status_code == 200
    resp = client.post("/api/v1/tags", json={"name": "Pop"}, headers=csrf(client))
    assert resp.status_code == 403
    assert "Schreibberechtigung" in resp.json()["detail"]


def test_required_group_blocks_login(make_client: ClientFactory, idp: FakeIdP, db: Session) -> None:
    client = make_client(oidc_required_group="musichub-users")
    resp = login(client, idp, groups=["other"])
    assert resp.headers["location"] == "/login/?error=forbidden"
    assert SESSION not in client.cookies
    assert db.scalar(select(UserSession)) is None
    ok = login(client, idp, groups=["musichub-users"])
    assert ok.headers["location"] == "/"


def test_groups_from_userinfo_when_missing_in_id_token(client: TestClient, idp: FakeIdP) -> None:
    idp.include_groups_in_id_token = False
    login(client, idp)
    assert client.get("/api/v1/me").json()["can_write"] is True


def test_bearer_tokens_still_work_without_csrf(client: TestClient, idp: FakeIdP) -> None:
    headers = {"Authorization": f"Bearer {idp.access_token()}"}
    resp = client.post("/api/v1/tags", json={"name": "Bearer"}, headers=headers)
    assert resp.status_code == 201
    assert client.get("/api/v1/me", headers=headers).json()["auth_method"] == "oidc"


# --- Fehlerfälle beim Rückruf ---------------------------------------------------------


def test_callback_with_unknown_state(client: TestClient) -> None:
    resp = client.get("/api/auth/callback", params={"code": "x", "state": "unknown"})
    assert resp.headers["location"] == "/login/?error=invalid_state"


def test_callback_state_is_single_use(client: TestClient, idp: FakeIdP) -> None:
    location, state = start_login(client)
    code = idp.authorize(location)
    assert (
        client.get("/api/auth/callback", params={"code": code, "state": state}).status_code == 303
    )
    again = client.get("/api/auth/callback", params={"code": code, "state": state})
    assert again.headers["location"] == "/login/?error=invalid_state"


def test_callback_from_other_browser_is_rejected(make_client: ClientFactory, idp: FakeIdP) -> None:
    """Login-CSRF: Ein fremder Rückruf-Link darf das Opfer nicht ins fremde Konto loggen."""
    attacker, victim = make_client(), make_client()
    location, state = start_login(attacker)
    code = idp.authorize(location, sub="attacker")
    resp = victim.get("/api/auth/callback", params={"code": code, "state": state})
    assert resp.headers["location"] == "/login/?error=invalid_state"
    assert SESSION not in victim.cookies


def test_callback_after_expiry(client: TestClient, idp: FakeIdP, db: Session) -> None:
    location, state = start_login(client)
    code = idp.authorize(location)
    db.execute(update(AuthLogin).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()
    resp = client.get("/api/auth/callback", params={"code": code, "state": state})
    assert resp.headers["location"] == "/login/?error=invalid_state"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"nonce": "other"}, "invalid_id_token"),
        ({"aud": "other-client"}, "invalid_id_token"),
        ({"iss": "https://evil.example/"}, "invalid_id_token"),
        ({"exp": 1}, "invalid_id_token"),
    ],
)
def test_invalid_id_tokens_are_rejected(
    client: TestClient, idp: FakeIdP, overrides: dict[str, Any], error: str
) -> None:
    idp.id_token_overrides = overrides
    resp = login(client, idp)
    assert resp.headers["location"] == f"/login/?error={error}"
    assert SESSION not in client.cookies


def test_tampered_pkce_verifier_fails(client: TestClient, idp: FakeIdP) -> None:
    location, state = start_login(client)
    code = idp.authorize(location)
    grant = idp.grants[code]
    grant.challenge = "A" * 43
    resp = client.get("/api/auth/callback", params={"code": code, "state": state})
    assert resp.headers["location"] == "/login/?error=token_exchange_failed"


def test_provider_errors(client: TestClient, idp: FakeIdP) -> None:
    _location, state = start_login(client)
    resp = client.get("/api/auth/callback", params={"error": "access_denied", "state": state})
    assert resp.headers["location"] == "/login/?error=access_denied"

    idp.token_status = 500
    assert login(client, idp).headers["location"] == "/login/?error=token_exchange_failed"

    idp.token_status = 200
    idp.down = True
    other = TestClient(client.app, base_url=PUBLIC_URL, follow_redirects=False)
    other.app.state.oidc._discovery = None  # Discovery-Cache leeren
    resp = other.get("/api/auth/login")
    assert resp.headers["location"] == "/login/?error=provider_unavailable"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/library/?q=1", "/library/?q=1"),
        ("https://evil.example/", "/"),
        ("//evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("/api/v1/tracks", "/"),
        ("library", "/"),
        (None, "/"),
    ],
)
def test_return_to_is_restricted_to_local_paths(value: str | None, expected: str) -> None:
    assert safe_return_to(value) == expected


def test_open_redirect_is_blocked_end_to_end(client: TestClient, idp: FakeIdP) -> None:
    resp = login(client, idp, "https://evil.example/")
    assert resp.headers["location"] == "/"


# --- Ablauf, Fixierung, Abmeldung -----------------------------------------------------


def test_idle_session_expires(client: TestClient, idp: FakeIdP, db: Session) -> None:
    login(client, idp)
    db.execute(update(UserSession).values(last_seen_at=datetime.now(UTC) - timedelta(hours=13)))
    db.commit()
    resp = client.get("/api/v1/me")
    assert resp.status_code == 401
    assert resp.json()["code"] == "session_expired"
    cleared = " ".join(resp.headers.get_list("set-cookie"))
    assert f"{SESSION}=" in cleared and "Max-Age=0" in cleared
    assert db.scalar(select(UserSession)) is None


def test_absolute_session_lifetime(client: TestClient, idp: FakeIdP, db: Session) -> None:
    login(client, idp)
    db.execute(update(UserSession).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    db.commit()
    assert client.get("/api/v1/tracks").json()["code"] == "session_expired"


def test_activity_extends_idle_timeout(client: TestClient, idp: FakeIdP, db: Session) -> None:
    login(client, idp)
    old = datetime.now(UTC) - timedelta(hours=11)
    db.execute(update(UserSession).values(last_seen_at=old))
    db.commit()
    assert client.get("/api/v1/me").status_code == 200
    db.expire_all()
    row = db.scalar(select(UserSession))
    assert row is not None and row.last_seen_at > old + timedelta(hours=10)


def test_new_login_replaces_existing_session(client: TestClient, idp: FakeIdP, db: Session) -> None:
    login(client, idp)
    first = client.cookies[SESSION]
    login(client, idp)
    assert client.cookies[SESSION] != first
    assert len(db.scalars(select(UserSession)).all()) == 1


def test_logout(client: TestClient, idp: FakeIdP, db: Session) -> None:
    login(client, idp)
    assert client.post("/api/auth/logout").status_code == 403  # ohne CSRF-Token
    resp = client.post("/api/auth/logout", headers=csrf(client))
    assert resp.status_code == 200
    assert resp.json()["redirect"].endswith("/application/o/musichub/end-session/")
    assert db.scalar(select(UserSession)) is None
    assert SESSION not in client.cookies
    assert client.get("/api/v1/me").status_code == 401


def test_logout_without_provider_logout(make_client: ClientFactory, idp: FakeIdP) -> None:
    client = make_client(oidc_logout_at_provider=False)
    login(client, idp)
    resp = client.post("/api/auth/logout", headers=csrf(client))
    assert resp.json()["redirect"] == "/login/?logged_out=1"


def test_purge_expired_sessions(client: TestClient, idp: FakeIdP, db: Session) -> None:
    login(client, idp)
    login(make_second := TestClient(client.app, base_url=PUBLIC_URL, follow_redirects=False), idp)
    assert make_second.cookies[SESSION]
    db.execute(
        update(UserSession)
        .where(UserSession.token_hash != "")
        .values(last_seen_at=datetime.now(UTC) - timedelta(days=1))
    )
    db.commit()
    assert purge_expired(db, oidc_settings()) == 2
    db.commit()
    assert db.scalar(select(UserSession)) is None


def test_login_is_rate_limited(make_client: ClientFactory) -> None:
    client = make_client(auth_rate_limit_per_minute=3)
    for _ in range(3):
        assert client.get("/api/auth/login").status_code == 302
    resp = client.get("/api/auth/login")
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) >= 1


# --- Konfiguration -------------------------------------------------------------------------


def test_dev_mode_session_endpoint(client_dev: TestClient) -> None:
    assert client_dev.get("/api/auth/session").json()["mode"] == "dev"
    resp = client_dev.get("/api/auth/login", follow_redirects=False)
    assert resp.status_code == 302 and resp.headers["location"] == "/settings/"


@pytest.fixture
def client_dev(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        yield c


def test_oidc_without_client_secret_only_accepts_bearer(
    make_client: ClientFactory, idp: FakeIdP
) -> None:
    client = make_client(oidc_client_secret=None)
    assert client.get("/api/auth/session").json()["login_enabled"] is False
    assert client.get("/api/auth/login").headers["location"] == "/login/?error=not_configured"
    headers = {"Authorization": f"Bearer {idp.access_token()}"}
    assert client.get("/api/v1/me", headers=headers).status_code == 200
