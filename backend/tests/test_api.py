"""API-Tests: Health, Fehlerformat, Authentifizierung und Track-CRUD."""

from __future__ import annotations

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from musichub.auth import current_principal, require_write


def _api_routes(routes) -> list[APIRoute]:  # type: ignore[no-untyped-def]
    """Alle APIRoutes, auch aus eingebundenen (ab FastAPI 0.16x geschachtelten) Routern."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif (nested := getattr(route, "original_router", None)) is not None:
            found.extend(_api_routes(nested.routes))
    return found


def _dependencies(route: APIRoute) -> set[object]:
    found: set[object] = set()
    stack = [route.dependant]
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            found.add(dep.call)
        stack.extend(dep.dependencies)
    return found


# --- Sicherheitsnetz über alle Routen -----------------------------------------


#: Ausnahmen mit Begründung. Abmelden muss auch ohne Schreibrecht gehen; die Route prüft
#: selbst den CSRF-Token der Sitzung (test_auth_session.py).
WRITE_EXEMPT = {"/api/auth/logout"}


def test_every_write_route_requires_write_permission(app) -> None:  # type: ignore[no-untyped-def]
    routes = _api_routes(app.routes)
    writes = [
        r for r in routes if r.methods - {"GET", "HEAD", "OPTIONS"} and r.path not in WRITE_EXEMPT
    ]
    assert writes, "Erwartet mindestens eine schreibende Route"
    unprotected = [f"{r.methods} {r.path}" for r in writes if require_write not in _dependencies(r)]
    assert not unprotected, f"Schreibende Routen ohne require_write: {unprotected}"


def test_every_v1_route_requires_authentication(app) -> None:  # type: ignore[no-untyped-def]
    routes = [r for r in _api_routes(app.routes) if r.path.startswith("/api/v1")]
    assert routes
    open_routes = [r.path for r in routes if current_principal not in _dependencies(r)]
    assert not open_routes


# --- Health & Fehlerbehandlung -------------------------------------------------


def test_live(client: TestClient) -> None:
    resp = client.get("/api/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
    assert len(resp.headers["x-request-id"]) == 32


def test_request_id_is_propagated(client: TestClient) -> None:
    resp = client.get("/api/health/live", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"


def test_ready_reports_unreachable_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    dead = create_engine(
        "postgresql+psycopg://x:x@127.0.0.1:1/x", connect_args={"connect_timeout": 1}
    )
    monkeypatch.setattr("musichub.api.routes.health.get_engine", lambda: dead)
    resp = client.get("/api/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["checks"]["database"]["status"] == "error"
    assert "x:x" not in resp.text  # keine Verbindungsdetails nach außen


@pytest.mark.db
def test_ready_ok_with_migrated_db(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    resp = client.get("/api/health/ready")
    assert resp.status_code == 200, resp.text
    assert resp.json()["checks"] == {
        "database": {"status": "ok", "detail": None},
        "migrations": {"status": "ok", "detail": None},
    }


def test_unknown_route_is_problem_json(client: TestClient) -> None:
    resp = client.get("/api/nope")
    assert resp.status_code == 404
    assert resp.headers["content-type"] == "application/problem+json"
    assert resp.json()["request_id"] == resp.headers["x-request-id"]


def test_unhandled_error_hides_details(app) -> None:  # type: ignore[no-untyped-def]
    @app.get("/api/_boom", include_in_schema=False)
    def boom() -> None:
        raise RuntimeError("geheimes Detail")

    try:
        with TestClient(app, raise_server_exceptions=False) as c:
            resp = c.get("/api/_boom")
        assert resp.status_code == 500
        assert resp.json()["code"] == "internal_error"
        assert "geheimes Detail" not in resp.text
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != "/api/_boom"
        ]


# --- Authentifizierung -----------------------------------------------------------


def test_v1_requires_token(client: TestClient) -> None:
    resp = client.get("/api/v1/me")
    assert resp.status_code == 401
    assert resp.headers["www-authenticate"] == "Bearer"


def test_wrong_token_rejected(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/tracks",
        json={"title": "x", "artist": "y"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


def test_me(client: TestClient, auth: dict[str, str]) -> None:
    resp = client.get("/api/v1/me", headers=auth)
    assert resp.status_code == 200
    assert resp.json()["auth_method"] == "dev"
    assert resp.json()["can_write"] is True


def test_connectors_status(client: TestClient, auth: dict[str, str]) -> None:
    resp = client.get("/api/v1/connectors", headers=auth)
    assert resp.status_code == 200
    assert {c["platform"]: c["state"] for c in resp.json()} == {
        "spotify": "not_configured",
        "soundcloud": "not_configured",
        "youtube": "not_configured",
    }


# --- Tracks ----------------------------------------------------------------------


@pytest.mark.db
def test_track_crud(client: TestClient, auth: dict[str, str], db) -> None:  # type: ignore[no-untyped-def]
    created = client.post(
        "/api/v1/tracks",
        json={
            "title": " Blinding Lights ",
            "artist": "The Weeknd",
            "duration_ms": 200040,
            "isrc": "usug11904206",
        },
        headers=auth,
    )
    assert created.status_code == 201, created.text
    track = created.json()
    assert track["title"] == "Blinding Lights"
    assert track["isrc"] == "USUG11904206"
    assert track["sources"] == []  # Track existiert ohne jede Plattform
    assert track["platforms"] == []

    tid = track["id"]
    assert client.get(f"/api/v1/tracks/{tid}", headers=auth).status_code == 200

    patched = client.patch(f"/api/v1/tracks/{tid}", json={"album": "After Hours"}, headers=auth)
    assert patched.json()["album"] == "After Hours"
    assert patched.json()["title"] == "Blinding Lights"

    listed = client.get("/api/v1/tracks", params={"q": "weeknd"}, headers=auth).json()
    assert listed["total"] == 1

    assert client.get("/api/v1/tracks", params={"q": "100%_"}, headers=auth).json()["total"] == 0

    assert client.delete(f"/api/v1/tracks/{tid}", headers=auth).status_code == 204
    missing = client.get(f"/api/v1/tracks/{tid}", headers=auth)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"


@pytest.mark.db
def test_track_validation(client: TestClient, auth: dict[str, str], db) -> None:  # type: ignore[no-untyped-def]
    resp = client.post(
        "/api/v1/tracks", json={"title": "", "artist": "x", "isrc": "nope"}, headers=auth
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "validation_error"
    assert {tuple(e["loc"])[-1] for e in body["errors"]} == {"title", "isrc"}
