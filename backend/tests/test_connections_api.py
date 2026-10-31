"""Plattform-Konten über API und Worker – mit nachgebildeten Plattform-APIs."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from fake_platforms import API_HOSTS, FakePlatforms, sp_track
from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings
from musichub.domain.types import JobStatus
from musichub.jobs.worker import run_once
from musichub.models import ConnectorAccount, Job, RemotePlaylist, Track

pytestmark = pytest.mark.db

KEY = "Hm9fiOmSQynb6d0_JpW2FxA-fnmzLHi4_a5yCIhE9A4="


@pytest.fixture
def fake() -> FakePlatforms:
    return FakePlatforms()


@pytest.fixture
def settings() -> Settings:
    return Settings(
        spotify_client_id="sp-id",
        spotify_client_secret="sp-secret",  # type: ignore[arg-type]
        youtube_client_id="yt-id",
        youtube_client_secret="yt-secret",  # type: ignore[arg-type]
        public_url="https://music.example.com",
        token_encryption_key=KEY,  # type: ignore[arg-type]
    )


@pytest.fixture
def registry(settings: Settings, fake: FakePlatforms) -> ConnectorRegistry:
    return ConnectorRegistry(default_connectors(settings, transport=fake.transport))


@pytest.fixture
def client(
    settings: Settings,
    registry: ConnectorRegistry,
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    from musichub.main import create_app

    monkeypatch.setattr("musichub.connections.importer.get_settings", lambda: settings)
    app = create_app(settings)
    app.state.connectors = registry
    with TestClient(app) as c:
        yield c


@pytest.fixture
def api(client: TestClient, auth: dict[str, str]):  # type: ignore[no-untyped-def]
    class Api:
        def __getattr__(self, method: str):  # type: ignore[no-untyped-def]
            def call(path: str, expected: int | None = None, **kw: Any) -> Any:
                resp = client.request(method.upper(), f"/api/v1{path}", headers=auth, **kw)
                if expected is not None:
                    assert resp.status_code == expected, resp.text
                return resp.json() if resp.content else None

            return call

    return Api()


def drain(registry: ConnectorRegistry, limit: int = 20) -> None:
    for _ in range(limit):
        if not run_once("test-worker", registry):
            return


def connect(api, client: TestClient, platform: str = "spotify") -> dict[str, str]:  # type: ignore[no-untyped-def]
    url = api.post(f"/connections/{platform}/authorize", 200)["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    resp = client.get(
        f"/api/oauth/callback/{platform}",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}


def remote(api, platform: str = "spotify") -> dict[str, dict]:  # type: ignore[no-untyped-def]
    return {p["external_id"]: p for p in api.get(f"/connections/{platform}/playlists", 200)}


# --- Status und Verbinden ------------------------------------------------------------------------


def test_status_without_configuration_names_missing_settings(
    client: TestClient, auth: dict[str, str], settings: Settings
) -> None:
    settings.token_encryption_key = None
    data = {c["platform"]: c for c in client.get("/api/v1/connections", headers=auth).json()}
    assert data["spotify"]["configured"] is False
    assert data["spotify"]["missing"] == ["MUSICHUB_TOKEN_ENCRYPTION_KEY"]
    assert "MUSICHUB_SOUNDCLOUD_CLIENT_ID" in data["soundcloud"]["missing"]
    assert data["spotify"]["read_only"] is True
    assert data["spotify"]["redirect_uri"] == "https://music.example.com/api/oauth/callback/spotify"
    resp = client.post("/api/v1/connections/soundcloud/authorize", headers=auth)
    assert resp.status_code == 422 and "MUSICHUB_SOUNDCLOUD_CLIENT_ID" in resp.json()["missing"]


def test_connect_stores_encrypted_tokens_and_never_returns_them(
    api, client: TestClient, db: Session, fake: FakePlatforms
) -> None:  # type: ignore[no-untyped-def]
    result = connect(api, client)
    assert result == {"platform": "spotify", "connected": "1"}
    account = db.scalar(select(ConnectorAccount))
    assert account is not None and account.display_name == "Silas"
    assert "access-1" not in account.access_token_enc
    assert "refresh-1" not in (account.refresh_token_enc or "")

    status = {c["platform"]: c for c in api.get("/connections", 200)}["spotify"]
    assert status["account"]["display_name"] == "Silas"
    assert status["account"]["scopes"] == ["playlist-read-private", "playlist-read-collaborative"]
    dumped = str(api.get("/connections", 200)) + str(api.get("/jobs", 200))
    assert "access-1" not in dumped and "refresh-1" not in dumped
    # Nach dem Verbinden wird die Playlist-Liste eingeplant
    assert [j["kind"] for j in api.get("/jobs", 200)] == ["platform.refresh_playlists"]


def test_callback_rejects_unknown_reused_and_denied_states(api, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    def callback(**params: str) -> dict[str, str]:
        resp = client.get("/api/oauth/callback/spotify", params=params, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["cache-control"] == "no-store"
        return {k: v[0] for k, v in parse_qs(urlsplit(resp.headers["location"]).query).items()}

    assert callback(state="forged", code="good-code")["error"] == "invalid_state"
    url = api.post("/connections/spotify/authorize", 200)["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert callback(state=state, error="access_denied")["error"] == "denied"
    # verbraucht: kein zweites Mal
    assert callback(state=state, code="good-code")["error"] == "invalid_state"
    url = api.post("/connections/spotify/authorize", 200)["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    assert callback(state=state, code="bad-code")["error"] == "exchange_failed"
    assert api.get("/connections", 200)[0]["account"] is None


def test_state_of_other_platform_is_rejected(api, client: TestClient) -> None:  # type: ignore[no-untyped-def]
    url = api.post("/connections/youtube/authorize", 200)["authorize_url"]
    state = parse_qs(urlsplit(url).query)["state"][0]
    resp = client.get(
        "/api/oauth/callback/spotify",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert "error=invalid_state" in resp.headers["location"]


# --- Playlists und Import ------------------------------------------------------------------------


def test_spotify_import_flow_incremental_and_read_only(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    existing = api.post(
        "/tracks",
        201,
        json={
            "title": "Song 2",
            "artist": "Artist",
            "sources": [{"url": "https://open.spotify.com/track/" + "0" * 21 + "2"}],
        },
    )
    connect(api, client)
    drain(registry)
    playlists = remote(api)
    own, foreign = playlists["pl_own"], playlists["pl_foreign"]
    assert own["items_accessible"] and not foreign["items_accessible"]
    assert own["local_playlist_id"] is None and not own["up_to_date"]

    # Fremde Playlist: im Development Mode nicht lesbar
    resp = api.post("/connections/spotify/import", 422, json={"playlist_ids": [foreign["id"]]})
    assert "Today's Top Hits" in resp["detail"]

    jobs = api.post("/connections/spotify/import", 202, json={"playlist_ids": [own["id"]]})
    assert jobs[0]["status"] == "queued" and jobs[0]["label"] == "Playlist importieren"
    # Zweimal einplanen erzeugt keinen zweiten Job
    again = api.post("/connections/spotify/import", 202, json={"playlist_ids": [own["id"]]})
    assert again[0]["id"] == jobs[0]["id"]
    drain(registry)

    job = api.get(f"/jobs/{jobs[0]['id']}", 200)
    assert job["status"] == "succeeded", job
    assert job["result"]["created"] == 2 and job["result"]["linked"] == 1
    assert job["result"]["skipped"] == 1
    assert job["progress"]["current"] == 4

    own = remote(api)["pl_own"]
    assert own["up_to_date"] and own["last_import_status"] == "ok"
    playlist = api.get(f"/playlists/{own['local_playlist_id']}", 200)
    assert playlist["name"] == "Warm-up"
    titles = [i["track"]["title"] for i in playlist["items"]]
    assert titles == ["Song 1", "Song 2", "Bootleg (lokal)"]
    assert playlist["items"][1]["track"]["id"] == existing["id"]  # erkannt, nicht doppelt
    song1 = playlist["items"][0]["track"]
    assert song1["isrc"] == "GBAAA2600001" and song1["release_date"] == "2024-05-17"
    assert song1["sources"][0]["availability"] == "available"
    run = api.get(f"/imports/{own['last_import_run_id']}", 200)
    assert run["format"] == "spotify_api" and run["status"] == "committed"
    assert any("übersprungen" in w for w in run["warnings"])
    assert api.get("/connections", 200)[0]["account"]["last_import_at"]

    # Unverändert: nichts wird gelesen
    before = len(fake.api_requests())
    api.post("/connections/spotify/import", 202, json={"playlist_ids": [own["id"]]})
    drain(registry)
    assert len(fake.api_requests()) == before
    assert remote(api)["pl_own"]["last_import_status"] == "unchanged"

    # Lokal umsortiert, auf Spotify kommt ein Track dazu: nur ergänzen
    items = playlist["items"]
    api.put(
        f"/playlists/{playlist['id']}/items/order",
        200,
        json={"item_ids": [items[2]["id"], items[0]["id"], items[1]["id"]]},
    )
    fake.sp_items.insert(0, {"is_local": False, "item": sp_track(3)})
    fake.sp_playlists[0]["snapshot_id"] = "snap2"
    api.post("/connections/spotify/playlists/refresh", 202, json={"import_changed": True})
    drain(registry)
    updated = api.get(f"/playlists/{playlist['id']}", 200)
    assert [i["track"]["title"] for i in updated["items"]] == [
        "Bootleg (lokal)",
        "Song 1",
        "Song 2",
        "Song 3",
    ]
    assert remote(api)["pl_own"]["up_to_date"]
    assert db.scalar(select(text("count(*)")).select_from(Track)) == 4

    # Nie etwas anderes als GET gegen die Plattform-API
    assert {r.method for r in fake.api_requests()} == {"GET"}
    assert all(r.url.host in API_HOSTS | {"accounts.spotify.com"} for r in fake.requests)
    # Nach dem Import werden Matching-Vorschläge eingeplant
    assert db.scalar(select(Job).where(Job.kind == "matching.scan")) is not None


def test_potential_duplicates_are_created_not_linked(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms
) -> None:  # type: ignore[no-untyped-def]
    # Tippfehler im lokalen Titel: nur „mögliches Duplikat“, kein eindeutiger Treffer
    local = api.post("/tracks", 201, json={"title": "Sonng 1", "artist": "Artist"})
    fake.sp_items = [{"is_local": False, "item": sp_track(1, name="Song 1")}]
    fake.sp_playlists = fake.sp_playlists[:1]
    connect(api, client)
    drain(registry)
    api.post(
        "/connections/spotify/import", 202, json={"playlist_ids": [remote(api)["pl_own"]["id"]]}
    )
    drain(registry)
    assert api.get(f"/tracks/{local['id']}", 200)["sources"] == []
    assert api.get("/tracks", 200)["total"] == 2


def test_rate_limit_reschedules_job_with_retry_after(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    import httpx

    connect(api, client)
    drain(registry)
    own = remote(api)["pl_own"]
    fake.overrides["/v1/playlists/pl_own/items"] = httpx.Response(
        429, headers={"Retry-After": "120"}
    )
    job_id = api.post("/connections/spotify/import", 202, json={"playlist_ids": [own["id"]]})[0][
        "id"
    ]
    drain(registry)
    job = db.get(Job, job_id)
    db.refresh(job)
    assert job is not None and job.status == JobStatus.FAILED.value
    wait = (job.run_after - datetime.now(UTC)).total_seconds()
    assert 100 < wait <= 121
    assert "Rate-Limit" in (job.last_error or "")
    assert remote(api)["pl_own"]["last_import_status"] == "error"

    # Später klappt es: Job wird wiederholt und erfolgreich
    del fake.overrides["/v1/playlists/pl_own/items"]
    db.execute(update(Job).where(Job.id == job.id).values(run_after=datetime.now(UTC)))
    db.commit()
    drain(registry)
    assert api.get(f"/jobs/{job_id}", 200)["status"] == "succeeded"
    assert remote(api)["pl_own"]["last_error"] is None


def test_expired_token_is_refreshed_and_revoked_token_requires_reconnect(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client)
    db.execute(
        update(ConnectorAccount).values(token_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    db.commit()
    drain(registry)
    account = db.scalar(select(ConnectorAccount))
    db.refresh(account)
    assert account is not None and account.status == "connected"
    assert account.token_expires_at and account.token_expires_at > datetime.now(UTC)
    assert fake.token_counter == 2  # Code-Tausch + Erneuerung

    fake.refresh_fails = True
    db.execute(
        update(ConnectorAccount).values(token_expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    db.commit()
    job = api.post("/connections/spotify/playlists/refresh", 202, json={})
    drain(registry)
    assert api.get(f"/jobs/{job['id']}", 200)["status"] == "dead"  # nicht wiederholbar
    status = api.get("/connections", 200)[0]
    assert status["account"]["status"] == "reauth_required"
    api.post("/connections/spotify/playlists/refresh", 409, json={})


def test_disconnect_keeps_imported_data(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, "youtube")
    drain(registry)
    (playlist,) = api.get("/connections/youtube/playlists", 200)
    api.post("/connections/youtube/import", 202, json={"playlist_ids": [playlist["id"]]})
    drain(registry)
    imported = remote(api, "youtube")["PLmix"]
    local = api.get(f"/playlists/{imported['local_playlist_id']}", 200)
    # gelöschtes Video übersprungen, Topic-Kanal als Artist bereinigt
    assert [(i["track"]["title"], i["track"]["artist"]) for i in local["items"]] == [
        ("Kerala (Official Video)", "Bonobo"),
        ("Midnight City", "M83VEVO"),
    ]
    assert local["items"][0]["track"]["duration_ms"] == 230_000

    assert api.delete("/connections/youtube", 200) == {"revoked": True}
    assert fake.revoked == ["refresh-1"]
    assert db.scalar(select(ConnectorAccount)) is None
    api.get(f"/playlists/{local['id']}", 200)
    assert remote(api, "youtube")["PLmix"]["local_playlist_id"] == local["id"]
    api.delete("/connections/youtube", 404)


def test_youtube_quota_exhaustion_postpones_job(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, "youtube")
    fake.yt_quota_exhausted = True
    drain(registry)
    job = db.scalar(select(Job).where(Job.kind == "platform.refresh_playlists"))
    assert job is not None
    db.refresh(job)
    assert job.status == "failed"
    assert (job.run_after - datetime.now(UTC)).total_seconds() > 60
    assert "Tageskontingent" in (job.last_error or "")


def test_refresh_metadata_fills_only_empty_fields(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client)
    known = api.post(
        "/tracks",
        201,
        json={
            "title": "Mein Titel",
            "artist": "Artist",
            "album": "Eigenes Album",
            "sources": [{"url": "https://open.spotify.com/track/" + "0" * 21 + "5"}],
        },
    )
    gone = api.post(
        "/tracks",
        201,
        json={
            "title": "Weg",
            "artist": "Artist",
            "sources": [{"url": "https://open.spotify.com/track/" + "0" * 21 + "9"}],
        },
    )
    job = api.post("/connections/spotify/refresh-metadata", 202)
    drain(registry)
    assert api.get(f"/jobs/{job['id']}", 200)["result"]["checked"] == 2
    track = api.get(f"/tracks/{known['id']}", 200)
    assert track["title"] == "Mein Titel" and track["album"] == "Eigenes Album"  # lokal führt
    assert track["isrc"] == "GBAAA2600005" and track["duration_ms"] == 205_000
    assert track["sources"][0]["availability"] == "available"
    assert api.get(f"/tracks/{gone['id']}", 200)["sources"][0]["availability"] == "unavailable"


def test_jobs_require_connection_and_existing_playlists(api) -> None:  # type: ignore[no-untyped-def]
    api.post("/connections/spotify/playlists/refresh", 409, json={})
    api.delete("/connections/spotify", 404)
    assert api.get("/connections/spotify/playlists", 200) == []
    api.get("/jobs/00000000-0000-0000-0000-000000000000", 404)


def test_remote_playlists_disappearing_are_marked_removed(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client)
    drain(registry)
    fake.sp_playlists = fake.sp_playlists[:1]
    api.post("/connections/spotify/playlists/refresh", 202, json={})
    drain(registry)
    assert set(remote(api)) == {"pl_own"}
    all_rows = api.get("/connections/spotify/playlists", 200, params={"include_removed": True})
    assert {p["external_id"]: p["removed"] for p in all_rows} == {
        "pl_own": False,
        "pl_foreign": True,
    }
    assert db.scalar(select(text("count(*)")).select_from(RemotePlaylist)) == 2
