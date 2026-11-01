"""Sicherheits- und Robustheitsprüfungen über die API (Eingaben, Fehler, Header, Uploads)."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db


def _no_internals(body: str) -> None:
    for marker in ("Traceback", "sqlalchemy", "psycopg", 'File "', "SELECT ", "INSERT "):
        assert marker not in body, f"interne Details in Antwort: {marker}"


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("POST", "/api/v1/tracks", {"title": "x" * 5000, "artist": "a"}),
        ("POST", "/api/v1/tracks", {"title": "", "artist": "a"}),
        ("POST", "/api/v1/tracks", {"title": "t", "artist": "a", "duration_ms": -5}),
        ("POST", "/api/v1/tracks", ["kein", "objekt"]),
        ("POST", "/api/v1/tags", {"name": "n" * 1000}),
        ("POST", "/api/v1/playlists", {"name": "p" * 1000}),
        ("GET", "/api/v1/tracks/not-a-uuid", None),
        ("GET", "/api/v1/tracks?limit=100000", None),
        ("GET", "/api/v1/tracks?sort=;DROP TABLE tracks", None),
        ("GET", "/api/v1/export/library?format=exe", None),
    ],
)
def test_invalid_input_is_rejected_cleanly(
    client: TestClient,
    db: object,
    auth: dict[str, str],
    method: str,
    path: str,
    payload: object,
) -> None:
    resp = client.request(method, path, json=payload, headers=auth)
    assert resp.status_code == 422, (path, resp.status_code, resp.text[:300])
    assert resp.headers["content-type"].startswith("application/problem+json")
    _no_internals(resp.text)


def test_sql_meta_characters_are_plain_data(
    client: TestClient, db: object, auth: dict[str, str]
) -> None:
    evil = "Robert'); DROP TABLE tracks;--"
    created = client.post("/api/v1/tracks", json={"title": evil, "artist": "x"}, headers=auth)
    assert created.status_code == 201
    found = client.get("/api/v1/tracks", params={"q": evil}, headers=auth).json()
    assert [t["title"] for t in found["items"]] == [evil]


def test_unknown_ids_are_404(client: TestClient, db: object, auth: dict[str, str]) -> None:
    resp = client.get(f"/api/v1/tracks/{uuid.uuid4()}", headers=auth)
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_unhandled_errors_do_not_leak_details() -> None:
    from fastapi import APIRouter

    from musichub.core.config import get_settings
    from musichub.main import create_app

    app = create_app(get_settings())
    router = APIRouter()

    @router.get("/api/test-boom")
    def boom() -> None:
        raise RuntimeError("geheimes Detail aus der Datenbank: password=xyz")

    app.include_router(router)
    with TestClient(app, raise_server_exceptions=False) as c:
        resp = c.get("/api/test-boom")
    assert resp.status_code == 500
    assert "geheim" not in resp.text and "password" not in resp.text
    assert resp.json()["code"] == "internal_error"
    assert resp.json()["request_id"]
    assert resp.headers["x-request-id"] == resp.json()["request_id"]


def test_wrong_auth_scheme_and_garbage_tokens(client: TestClient, db: object) -> None:
    for header in ("Basic YWRtaW46YWRtaW4=", "Bearer ", "Bearer x" * 200, "Token abc"):
        resp = client.get("/api/v1/tracks", headers={"Authorization": header})
        assert resp.status_code == 401, header
        _no_internals(resp.text)


def test_upload_limits_and_binary_files(
    client: TestClient,
    db: object,
    auth: dict[str, str],
    app,  # type: ignore[no-untyped-def]
) -> None:
    max_bytes = app.state.settings.import_max_bytes
    big = b"a,b\n" * (max_bytes // 4 + 10)
    resp = client.post(
        "/api/v1/imports", files={"file": ("gross.csv", big, "text/csv")}, headers=auth
    )
    assert resp.status_code == 413
    binary = bytes(range(256)) * 50
    resp = client.post(
        "/api/v1/imports",
        files={"file": ("../../etc/passwd.csv", binary, "text/csv")},
        headers=auth,
    )
    assert 400 <= resp.status_code < 500, resp.text[:200]
    _no_internals(resp.text)


def test_upload_filename_is_not_used_as_path(
    client: TestClient, db: object, auth: dict[str, str]
) -> None:
    content = b"Track name,Artist name,Album,Playlist name,Type,ISRC\nA,B,C,P,Playlist,\n"
    resp = client.post(
        "/api/v1/imports",
        files={"file": ("../../../tmp/evil.csv", content, "text/csv")},
        headers=auth,
    )
    assert resp.status_code in (200, 201), resp.text[:300]
    # Name wird nur als Text gespeichert und angezeigt
    assert "evil.csv" in resp.json()["filename"]


def test_security_headers_on_api(client: TestClient, db: object, auth: dict[str, str]) -> None:
    resp = client.get("/api/v1/tracks", headers=auth)
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"
    assert "server" not in {k.lower() for k in resp.headers}


def test_cors_is_closed_by_default(client: TestClient, db: object) -> None:
    resp = client.options(
        "/api/v1/tracks",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}
