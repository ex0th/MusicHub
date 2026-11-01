"""Synchronisierung MusicHub → Plattform über API und Worker (nachgebildete Plattform-APIs).

Schwerpunkte: Sicherheitsfreigaben, Dry-Run, Bestätigung, Konflikte mit externen
Änderungen, Duplikate, fehlende Tracks, Fehlerverhalten ohne unkontrollierte Wiederholung
und Idempotenz.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from fake_platforms import FakePlatforms, sp_track
from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import Settings
from musichub.jobs.worker import run_once, schedule_due_syncs
from musichub.models import Job, PlaylistTarget, SyncRun

pytestmark = pytest.mark.db

KEY = "Hm9fiOmSQynb6d0_JpW2FxA-fnmzLHi4_a5yCIhE9A4="
SP_WRITE = (
    "playlist-read-private playlist-read-collaborative playlist-modify-private "
    "playlist-modify-public"
)
YT_WRITE = "https://www.googleapis.com/auth/youtube"


def sid(n: int) -> str:
    return f"{n:0>22}"


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
        sync_write_platforms="spotify,youtube",
    )


@pytest.fixture
def registry(settings: Settings, fake: FakePlatforms) -> ConnectorRegistry:
    return ConnectorRegistry(default_connectors(settings, transport=fake.transport))


@pytest.fixture
def client(
    settings: Settings, registry: ConnectorRegistry, db: Session, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    from musichub.main import create_app

    for module in ("connections.importer", "sync.executor"):
        monkeypatch.setattr(f"musichub.{module}.get_settings", lambda: settings)
    monkeypatch.setattr("musichub.jobs.worker.get_settings", lambda: settings)
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


def drain(registry: ConnectorRegistry, limit: int = 30) -> None:
    for _ in range(limit):
        if not run_once("test-worker", registry):
            return


def connect(
    api, client: TestClient, fake: FakePlatforms, platform: str = "spotify", *, write: bool = True
) -> None:  # type: ignore[no-untyped-def]
    fake.token_scope = (SP_WRITE if platform == "spotify" else YT_WRITE) if write else None
    url = api.post(
        f"/connections/{platform}/authorize", 200, params={"access": "write" if write else "read"}
    )["authorize_url"]
    q = parse_qs(urlsplit(url).query)
    if write and platform == "spotify":
        assert "playlist-modify-private" in q["scope"][0]
    state = q["state"][0]
    resp = client.get(
        f"/api/oauth/callback/{platform}",
        params={"state": state, "code": "good-code"},
        follow_redirects=False,
    )
    assert "connected=1" in resp.headers["location"]


def track(api, title: str, artist: str = "Artist", **kw: Any) -> dict:  # type: ignore[no-untyped-def]
    return api.post("/tracks", 201, json={"title": title, "artist": artist, **kw})


def sp_link(n: int, **kw: Any) -> dict[str, Any]:
    return {"url": f"https://open.spotify.com/track/{sid(n)}", **kw}


def library(api) -> dict[str, dict]:  # type: ignore[no-untyped-def]
    """Song 1–2 verknüpft, Song 5 nur per ISRC auffindbar, ein unauffindbarer Track mit
    YouTube-Link, ein als unverfügbar markierter Spotify-Track."""
    return {
        "s1": track(api, "Song 1", duration_ms=201_000, sources=[sp_link(1)]),
        "s2": track(api, "Song 2", duration_ms=202_000, sources=[sp_link(2)]),
        "s5": track(api, "Song 5", duration_ms=205_000, isrc="GBAAA2600005"),
        "rare": track(
            api,
            "White Label Dub",
            "Unbekannt",
            sources=[{"url": "https://www.youtube.com/watch?v=vidZZZZZZZZ"}],
        ),
        "gone": track(api, "Song 8", sources=[sp_link(8, availability="unavailable")]),
    }


def playlist(api, name: str, *tracks: dict) -> dict:  # type: ignore[no-untyped-def]
    return api.post("/playlists", 201, json={"name": name, "track_ids": [t["id"] for t in tracks]})


def target(api, pl: dict, expected: int = 201, **body: Any) -> dict:  # type: ignore[no-untyped-def]
    return api.post(
        f"/playlists/{pl['id']}/sync/targets",
        expected,
        json={"platform": "spotify", "create_new": True, **body},
    )


def preview(api, registry, pl: dict, tgt: dict) -> dict:  # type: ignore[no-untyped-def]
    run = api.post(f"/playlists/{pl['id']}/sync/targets/{tgt['id']}/preview", 202)
    assert run["dry_run"] and run["trigger"] == "preview"
    drain(registry)
    return api.get(f"/playlists/{pl['id']}/sync/runs/{run['id']}", 200)


def sync(api, registry, pl: dict, tgt: dict, expected: int = 202, **body: Any) -> dict:  # type: ignore[no-untyped-def]
    run = api.post(f"/playlists/{pl['id']}/sync/targets/{tgt['id']}/sync", expected, json=body)
    if expected != 202:
        return run
    drain(registry)
    return api.get(f"/playlists/{pl['id']}/sync/runs/{run['id']}", 200)


def sp_ids(fake: FakePlatforms, pid: str) -> list[str]:
    content = fake.sp_items if pid == "pl_own" else fake.sp_contents[pid]
    return [e["item"]["id"] for e in content if e.get("item")]


def writes(fake: FakePlatforms) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _ in fake.writes]


# --- Freigaben -----------------------------------------------------------------------------------


def test_writing_requires_explicit_configuration(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, settings: Settings
) -> None:  # type: ignore[no-untyped-def]
    lib = library(api)
    pl = playlist(api, "Set", lib["s1"])
    # Nur lesend verbunden
    connect(api, client, fake, write=False)
    tgt = target(api, pl)
    assert tgt["write_ready"] is False
    assert any("nur lesend" in b for b in tgt["write_blockers"])
    # Vorschau funktioniert trotzdem und schreibt nichts
    run = preview(api, registry, pl, tgt)
    assert run["status"] == "preview"
    assert fake.writes == []
    resp = sync(api, registry, pl, tgt, 409, preview_run_id=run["id"])
    assert any("nur lesend" in b for b in resp["blockers"])

    # Schreiben global nicht freigeschaltet
    settings.sync_write_platforms = ""
    api.post("/connections/spotify/authorize", 422, params={"access": "write"})
    tgt = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]
    assert any("MUSICHUB_SYNC_WRITE_PLATFORMS" in b for b in tgt["write_blockers"])
    status = {c["platform"]: c for c in api.get("/connections", 200)}["spotify"]
    assert status["read_only"] is True and status["write_enabled"] is False
    assert fake.writes == []


def test_disabled_target_does_not_sync(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "Set", track(api, "Song 1", sources=[sp_link(1)]))
    tgt = target(api, pl, enabled=False)
    run = preview(api, registry, pl, tgt)
    resp = sync(api, registry, pl, tgt, 409, preview_run_id=run["id"])
    assert any("deaktiviert" in b for b in resp["blockers"])
    assert fake.writes == []


# --- Vorschau, Bestätigung, fehlende Tracks -------------------------------------------------------


def test_preview_then_confirmed_first_sync(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    lib = library(api)
    pl = playlist(api, "Warm-up", lib["s1"], lib["rare"], lib["s5"], lib["gone"], lib["s2"])
    tgt = target(api, pl, remote_name="Warm-up (MusicHub)")
    assert tgt["managed"] and tgt["needs_confirmation"] and tgt["write_ready"]

    run = preview(api, registry, pl, tgt)
    plan = run["plan"]
    assert run["status"] == "preview" and fake.writes == []  # Dry-Run
    assert plan["create_playlist"] and plan["first_sync"]
    assert [(a["title"], a["new_link"]) for a in plan["add"]] == [
        ("Song 1", False),
        ("Song 5", True),  # per ISRC gefunden, wird beim Sync verknüpft
        ("Song 2", False),
    ]
    reasons = {u["title"]: u["reason"] for u in plan["unresolved"]}
    assert reasons == {"White Label Dub": "not_found", "Song 8": "unavailable"}
    rare = next(u for u in plan["unresolved"] if u["title"] == "White Label Dub")
    assert rare["alternatives"][0]["platform"] == "youtube"  # bekannter Alternativ-Link
    assert run["unmatched"] == 2
    # Vorschau verknüpft lokal noch nichts
    assert api.get(f"/tracks/{lib['s5']['id']}", 200)["sources"] == []

    # Ohne bestätigte Vorschau kein erster Sync
    resp = sync(api, registry, pl, tgt, 409)
    assert resp["needs_confirmation"] is True
    done = sync(api, registry, pl, tgt, preview_run_id=run["id"])
    assert done["status"] == "succeeded", done
    assert (done["added"], done["removed"], done["unmatched"]) == (3, 0, 2)
    assert done["started_at"] and done["finished_at"]

    t = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]
    assert (
        t["remote_playlist_id"] and t["last_status"] == "succeeded" and not t["needs_confirmation"]
    )
    assert sp_ids(fake, t["remote_playlist_id"]) == [sid(1), sid(5), sid(2)]
    assert writes(fake) == [
        ("POST", "/me/playlists"),
        ("POST", f"/playlists/{t['remote_playlist_id']}/items"),
    ]
    # Gefundener Track ist jetzt lokal verknüpft; fehlende Tracks bleiben lokal erhalten
    s5 = api.get(f"/tracks/{lib['s5']['id']}", 200)
    assert s5["sources"][0]["external_id"] == sid(5) and s5["sources"][0]["link_status"] == "linked"
    assert len(api.get(f"/playlists/{pl['id']}", 200)["items"]) == 5

    # Zweiter Lauf: nichts zu tun, keine weiteren Schreibzugriffe (idempotent)
    before = len(fake.writes)
    again = sync(api, registry, pl, tgt)
    assert again["status"] == "no_changes" and len(fake.writes) == before

    history = api.get(f"/playlists/{pl['id']}/sync/runs", 200)
    assert [r["status"] for r in history["items"]] == ["no_changes", "succeeded", "preview"]
    assert (
        history["items"][1]["remote_name"] == "Warm-up (MusicHub)"
        or history["items"][1]["remote_playlist_id"]
    )


def test_unresolved_list_and_recheck(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    lib = library(api)
    pl = playlist(api, "Set", lib["s1"], lib["rare"], lib["gone"], lib["s5"])
    unresolved = {
        u["title"]: u["reason"]
        for u in api.get(
            f"/playlists/{pl['id']}/sync/unresolved", 200, params={"platform": "spotify"}
        )
    }
    assert unresolved == {
        "White Label Dub": "unchecked",
        "Song 8": "unavailable",
        "Song 5": "unchecked",
    }
    preview(api, registry, pl, target(api, pl))
    rows = api.get(f"/playlists/{pl['id']}/sync/unresolved", 200, params={"platform": "spotify"})
    assert {u["title"]: u["reason"] for u in rows} == {
        "White Label Dub": "not_found",
        "Song 8": "unavailable",
    }
    rare = next(u for u in rows if u["title"] == "White Label Dub")
    assert rare["checked_at"] and rare["alternatives"][0]["platform"] == "youtube"
    reset = api.post(
        f"/playlists/{pl['id']}/sync/recheck",
        200,
        params={"platform": "spotify"},
        json={"track_ids": [rare["track_id"]]},
    )
    assert reset == {"reset": 1}
    rows = api.get(f"/playlists/{pl['id']}/sync/unresolved", 200, params={"platform": "spotify"})
    assert {u["title"]: u["reason"] for u in rows}["White Label Dub"] == "unchecked"


def test_search_api_error_is_distinguished(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "Set", track(api, "Song 3", duration_ms=203_000))
    fake.overrides["/v1/search"] = httpx.Response(500, json={})
    run = preview(api, registry, pl, target(api, pl))
    assert run["status"] == "preview"
    assert run["plan"]["unresolved"][0]["reason"] == "api_error"
    # Beim nächsten Lauf wird erneut gesucht
    del fake.overrides["/v1/search"]
    tgt = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]
    run = preview(api, registry, pl, tgt)
    assert run["plan"]["unresolved"] == [] and run["plan"]["add"][0]["new_link"]


def test_only_certain_search_hits_are_used(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    # Gleicher Titel/Artist, aber andere Version → nur Vorschlag, wird nicht übertragen
    fake.sp_catalog["0" * 20 + "77"] = sp_track(77, name="Song 77", duration_ms=300_000)
    local = track(api, "Song 77", version="Live", duration_ms=300_000)
    pl = playlist(api, "Set", local)
    run = preview(api, registry, pl, target(api, pl))
    assert run["plan"]["add"] == []
    assert run["plan"]["unresolved"][0]["reason"] in {"candidate", "not_found"}


# --- add_only: externe Tracks bleiben, extern Entferntes wird respektiert -------------------------


def test_add_only_into_existing_playlist(
    api, client: TestClient, registry: ConnectorRegistry, fake: FakePlatforms
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    drain(registry)  # Playlist-Liste
    own = {p["external_id"]: p for p in api.get("/connections/spotify/playlists", 200)}["pl_own"]
    s2 = track(api, "Song 2", sources=[sp_link(2)])
    s3 = track(api, "Song 3", sources=[sp_link(3)])
    pl = playlist(api, "Lokal", s2, s3)
    # Vorhandene Playlist: vollständig abgleichen ist nicht erlaubt
    resp = target(api, pl, 422, create_new=False, remote_playlist_id=own["id"], mirror_allowed=True)
    assert "Vorhandene Playlists" in resp["detail"]
    tgt = target(api, pl, create_new=False, remote_playlist_id=own["id"])
    assert not tgt["managed"] and tgt["mode"] == "add_only"
    before_external = sp_ids(fake, "pl_own")  # Song 1, Song 2

    run = preview(api, registry, pl, tgt)
    assert [a["title"] for a in run["plan"]["add"]] == ["Song 3"]
    assert run["plan"]["already_present"] == 1 and run["plan"]["remove"] == []
    done = sync(api, registry, pl, tgt, preview_run_id=run["id"])
    assert done["status"] == "succeeded"
    assert sp_ids(fake, "pl_own") == [*before_external, sid(3)]
    assert all(m == "POST" for m, _ in writes(fake))  # kein Ersetzen, kein Entfernen

    # Jemand entfernt Song 3 auf Spotify → wird nicht erneut hinzugefügt
    fake.sp_items[:] = [e for e in fake.sp_items if (e.get("item") or {}).get("id") != sid(3)]
    count = len(fake.writes)
    again = sync(api, registry, pl, tgt)
    assert again["status"] == "no_changes" and len(fake.writes) == count
    run = preview(api, registry, pl, tgt)
    assert [e["external_id"] for e in run["plan"]["not_readded"]] == [sid(3)]
    # auch beim übernächsten Lauf nicht
    assert sync(api, registry, pl, tgt)["status"] == "no_changes"
    assert sid(3) not in sp_ids(fake, "pl_own")


def test_same_remote_playlist_cannot_be_target_twice(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    drain(registry)
    own = {p["external_id"]: p for p in api.get("/connections/spotify/playlists", 200)}["pl_own"]
    a = playlist(api, "A")
    b = playlist(api, "B")
    target(api, a, create_new=False, remote_playlist_id=own["id"])
    target(api, b, 409, create_new=False, remote_playlist_id=own["id"])


# --- Duplikate -----------------------------------------------------------------------------------


def test_duplicates_add_only_once_mirror_exact(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    s1 = track(api, "Song 1", sources=[sp_link(1)])
    s2 = track(api, "Song 2", sources=[sp_link(2)])
    pl = playlist(api, "Dup", s1, s2, s1)
    add_only = target(api, pl)
    run = preview(api, registry, pl, add_only)
    assert run["plan"]["duplicates_skipped"] == 1
    sync(api, registry, pl, add_only, preview_run_id=run["id"])
    t = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]
    assert sp_ids(fake, t["remote_playlist_id"]) == [sid(1), sid(2)]

    mirror = target(api, pl, mirror_allowed=True, mode="mirror")
    run = preview(api, registry, pl, mirror)
    sync(api, registry, pl, mirror, preview_run_id=run["id"])
    t = next(
        x for x in api.get(f"/playlists/{pl['id']}/sync/targets", 200) if x["id"] == mirror["id"]
    )
    assert sp_ids(fake, t["remote_playlist_id"]) == [sid(1), sid(2), sid(1)]
    # Wiederholung erzeugt keine weiteren Duplikate
    again = sync(api, registry, pl, mirror)
    assert again["status"] == "no_changes"
    assert sp_ids(fake, t["remote_playlist_id"]) == [sid(1), sid(2), sid(1)]


# --- mirror: nur verwaltet + freigegeben, Konflikte mit externen Änderungen ----------------------


def test_mirror_mode_rules(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "M")
    target(api, pl, 422, mode="mirror")  # ohne Freigabe
    tgt = target(api, pl)
    api.patch(f"/playlists/{pl['id']}/sync/targets/{tgt['id']}", 422, json={"mode": "mirror"})
    tgt = api.patch(
        f"/playlists/{pl['id']}/sync/targets/{tgt['id']}", 200, json={"mirror_allowed": True}
    )
    tgt = api.patch(f"/playlists/{pl['id']}/sync/targets/{tgt['id']}", 200, json={"mode": "mirror"})
    assert tgt["mode"] == "mirror" and tgt["needs_confirmation"]
    tgt = api.patch(
        f"/playlists/{pl['id']}/sync/targets/{tgt['id']}", 200, json={"mirror_allowed": False}
    )
    assert tgt["mode"] == "add_only"


def test_mirror_detects_external_changes_and_needs_approval(
    api, client, registry, fake, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    s1 = track(api, "Song 1", sources=[sp_link(1)])
    s2 = track(api, "Song 2", sources=[sp_link(2)])
    s3 = track(api, "Song 3", sources=[sp_link(3)])
    pl = playlist(api, "Mirror", s1, s2, s3)
    tgt = target(api, pl, mirror_allowed=True, mode="mirror")
    sync(api, registry, pl, tgt, preview_run_id=preview(api, registry, pl, tgt)["id"])
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]

    # Lokal: Song 3 entfernt, umsortiert. Extern: jemand fügt Song 9 hinzu.
    items = api.get(f"/playlists/{pl['id']}", 200)["items"]
    api.delete(f"/playlists/{pl['id']}/items/{items[2]['id']}", 200)
    api.post(f"/playlists/{pl['id']}/items/{items[1]['id']}/move", 200, json={"position": 0})
    fake.sp_contents[remote_id].append({"is_local": False, "item": sp_track(9)})

    count = len(fake.writes)
    blocked = sync(api, registry, pl, tgt)
    assert blocked["status"] == "conflict" and len(fake.writes) == count  # nichts verändert
    assert sp_ids(fake, remote_id) == [sid(1), sid(2), sid(3), sid(9)]

    run = preview(api, registry, pl, tgt)
    plan = run["plan"]
    assert plan["requires_approval"]
    assert {(r["external_id"], r["external"]) for r in plan["remove"]} == {
        (sid(3), False),
        (sid(9), True),
    }
    assert [e["external_id"] for e in plan["external_added"]] == [sid(9)]
    # Zustimmung nur mit gesehener Vorschau
    api.post(
        f"/playlists/{pl['id']}/sync/targets/{tgt['id']}/sync",
        422,
        json={"approve_external_changes": True},
    )
    # Ohne Zustimmung, aber mit Vorschau: weiterhin Konflikt
    assert sync(api, registry, pl, tgt, preview_run_id=run["id"])["status"] == "conflict"
    done = sync(api, registry, pl, tgt, preview_run_id=run["id"], approve_external_changes=True)
    assert done["status"] == "succeeded" and done["removed"] == 2
    assert sp_ids(fake, remote_id) == [sid(2), sid(1)]


def test_change_after_preview_requires_new_preview(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    s1 = track(api, "Song 1", sources=[sp_link(1)])
    pl = playlist(api, "P", s1)
    tgt = target(api, pl, mirror_allowed=True, mode="mirror")
    sync(api, registry, pl, tgt, preview_run_id=preview(api, registry, pl, tgt)["id"])
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    fake.sp_contents[remote_id].append({"is_local": False, "item": sp_track(9)})
    run = preview(api, registry, pl, tgt)
    # Nach der Vorschau kommt extern noch ein Track dazu
    fake.sp_contents[remote_id].append({"is_local": False, "item": sp_track(10)})
    result = sync(api, registry, pl, tgt, preview_run_id=run["id"], approve_external_changes=True)
    assert result["status"] == "conflict"
    assert "seit der Vorschau" in result["errors"][0]
    assert sp_ids(fake, remote_id) == [sid(1), sid(9), sid(10)]


# --- Fehlerverhalten ------------------------------------------------------------------------------


def test_rate_limit_on_add_requeues_and_completes(api, client, registry, fake, db: Session) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(
        api,
        "P",
        track(api, "Song 1", sources=[sp_link(1)]),
        track(api, "Song 2", sources=[sp_link(2)]),
    )
    tgt = target(api, pl)
    run = preview(api, registry, pl, tgt)
    original = fake._spotify_write

    def limited(request: httpx.Request, path: str) -> httpx.Response:
        if path.endswith("/items"):
            return httpx.Response(429, headers={"Retry-After": "120"})
        return original(request, path)

    fake._spotify_write = limited  # type: ignore[method-assign]
    sync_run = api.post(
        f"/playlists/{pl['id']}/sync/targets/{tgt['id']}/sync",
        202,
        json={"preview_run_id": run["id"]},
    )
    drain(registry)
    state = api.get(f"/playlists/{pl['id']}/sync/runs/{sync_run['id']}", 200)
    assert state["status"] == "queued" and "Rate-Limit" in state["errors"][0]
    job = db.get(Job, db.scalar(select(SyncRun.job_id).where(SyncRun.id == sync_run["id"])))
    db.refresh(job)
    assert job is not None and job.status == "failed" and job.attempts == 1
    assert 100 < (job.run_after - datetime.now(UTC)).total_seconds() <= 121
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    assert remote_id and sp_ids(fake, remote_id) == []

    # Später: genau ein neuer Versuch, plant neu, keine Duplikate, kein zweites Anlegen
    fake._spotify_write = original  # type: ignore[method-assign]
    db.execute(update(Job).where(Job.id == job.id).values(run_after=datetime.now(UTC)))
    db.commit()
    drain(registry)
    done = api.get(f"/playlists/{pl['id']}/sync/runs/{sync_run['id']}", 200)
    assert done["status"] == "succeeded"
    assert sp_ids(fake, remote_id) == [sid(1), sid(2)]
    assert [p for m, p in writes(fake) if p == "/me/playlists"] == ["/me/playlists"]


def test_write_error_stops_without_retry(api, client, registry, fake, db: Session) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "P", track(api, "Song 1", sources=[sp_link(1)]))
    tgt = target(api, pl)
    run = preview(api, registry, pl, tgt)
    attempts: list[str] = []

    def broken(request: httpx.Request, path: str) -> httpx.Response:
        attempts.append(path)
        return httpx.Response(500, json={"error": {"status": 500, "message": "boom"}})

    fake._spotify_write = broken  # type: ignore[method-assign]
    sync_run = api.post(
        f"/playlists/{pl['id']}/sync/targets/{tgt['id']}/sync",
        202,
        json={"preview_run_id": run["id"]},
    )
    drain(registry)
    state = api.get(f"/playlists/{pl['id']}/sync/runs/{sync_run['id']}", 200)
    assert state["status"] == "failed" and "Schreibzugriff fehlgeschlagen" in state["errors"][0]
    assert attempts == ["/me/playlists"]  # genau ein Versuch, keine Wiederholung
    job = db.get(Job, db.scalar(select(SyncRun.job_id).where(SyncRun.id == sync_run["id"])))
    db.refresh(job)
    assert job is not None and job.status == "dead"
    t = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]
    assert t["last_status"] == "failed" and t["last_error"]
    # Lokale Daten unverändert
    assert len(api.get(f"/playlists/{pl['id']}", 200)["items"]) == 1


# --- Zeitplan ------------------------------------------------------------------------------------


def test_scheduled_sync_only_after_confirmation(api, client, registry, fake, db: Session) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "P", track(api, "Song 1", sources=[sp_link(1)]))
    tgt = target(api, pl, schedule_minutes=60)
    db.execute(update(PlaylistTarget).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    assert schedule_due_syncs(registry) == 0  # noch nicht bestätigt
    sync(api, registry, pl, tgt, preview_run_id=preview(api, registry, pl, tgt)["id"])
    db.execute(update(PlaylistTarget).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    api.post(
        f"/playlists/{pl['id']}/items",
        200,
        json={"track_ids": [track(api, "Song 2", sources=[sp_link(2)])["id"]]},
    )
    assert schedule_due_syncs(registry) == 1
    assert schedule_due_syncs(registry) == 0  # nächster Termin liegt in der Zukunft
    drain(registry)
    history = api.get(f"/playlists/{pl['id']}/sync/runs", 200)["items"]
    assert history[0]["trigger"] == "scheduled" and history[0]["status"] == "succeeded"
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    assert sp_ids(fake, remote_id) == [sid(1), sid(2)]


def test_scheduled_mirror_never_approves_external_changes(
    api, client, registry, fake, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "P", track(api, "Song 1", sources=[sp_link(1)]))
    tgt = target(api, pl, mirror_allowed=True, mode="mirror", schedule_minutes=60)
    sync(api, registry, pl, tgt, preview_run_id=preview(api, registry, pl, tgt)["id"])
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    fake.sp_contents[remote_id].append({"is_local": False, "item": sp_track(9)})
    db.execute(update(PlaylistTarget).values(next_run_at=datetime.now(UTC) - timedelta(minutes=1)))
    db.commit()
    assert schedule_due_syncs(registry) == 1
    drain(registry)
    assert api.get(f"/playlists/{pl['id']}/sync/runs", 200)["items"][0]["status"] == "conflict"
    assert sp_ids(fake, remote_id) == [sid(1), sid(9)]


# --- YouTube -------------------------------------------------------------------------------------


def yt(api, title: str, vid: str) -> dict:  # type: ignore[no-untyped-def]
    return track(api, title, sources=[{"url": f"https://www.youtube.com/watch?v={vid}"}])


def test_youtube_add_then_mirror_with_moves_and_removes(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    for vid, title in [("vidDDDDDDDD", "D"), ("vidEEEEEEEE", "E")]:
        fake.yt_catalog[vid] = (title, "Artist", "PT3M")
    connect(api, client, fake, "youtube")
    a = yt(api, "Kerala", "vidAAAAAAAA")
    c = yt(api, "Midnight City", "vidCCCCCCCC")
    d = yt(api, "D", "vidDDDDDDDD")
    pl = playlist(api, "Video-Set", a, c, d)
    tgt = api.post(
        f"/playlists/{pl['id']}/sync/targets",
        201,
        json={"platform": "youtube", "create_new": True, "mirror_allowed": True, "mode": "mirror"},
    )
    run = preview(api, registry, pl, tgt)
    assert run["plan"]["quota_units"] == 200  # Anlegen + 3 Einträge à 50 Einheiten
    sync(api, registry, pl, tgt, preview_run_id=run["id"])
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    assert [v for _, v in fake.yt_contents[remote_id]] == [
        "vidAAAAAAAA",
        "vidCCCCCCCC",
        "vidDDDDDDDD",
    ]

    # Lokal: C entfernen, E hinzufügen, D nach vorn
    items = api.get(f"/playlists/{pl['id']}", 200)["items"]
    api.delete(f"/playlists/{pl['id']}/items/{items[1]['id']}", 200)
    e = yt(api, "E", "vidEEEEEEEE")
    api.post(f"/playlists/{pl['id']}/items", 200, json={"track_ids": [e["id"]]})
    items = api.get(f"/playlists/{pl['id']}", 200)["items"]
    api.post(f"/playlists/{pl['id']}/items/{items[1]['id']}/move", 200, json={"position": 0})
    done = sync(api, registry, pl, tgt)
    assert done["status"] == "succeeded", done
    assert [v for _, v in fake.yt_contents[remote_id]] == [
        "vidDDDDDDDD",
        "vidAAAAAAAA",
        "vidEEEEEEEE",
    ]
    assert {m for m, _ in writes(fake)} == {"POST", "DELETE", "PUT"}
    assert sync(api, registry, pl, tgt)["status"] == "no_changes"


def test_youtube_quota_exhaustion_is_partial_and_resumes(
    api, client, registry, fake, db: Session
) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake, "youtube")
    pl = playlist(
        api, "P", yt(api, "Kerala", "vidAAAAAAAA"), yt(api, "Midnight City", "vidCCCCCCCC")
    )
    tgt = api.post(
        f"/playlists/{pl['id']}/sync/targets", 201, json={"platform": "youtube", "create_new": True}
    )
    run = preview(api, registry, pl, tgt)
    fake.yt_write_budget = 100  # reicht für Anlegen + 1 Eintrag
    sync_run = api.post(
        f"/playlists/{pl['id']}/sync/targets/{tgt['id']}/sync",
        202,
        json={"preview_run_id": run["id"]},
    )
    drain(registry)
    state = api.get(f"/playlists/{pl['id']}/sync/runs/{sync_run['id']}", 200)
    assert state["status"] == "queued" and "Kontingent" in state["errors"][0]
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    assert [v for _, v in fake.yt_contents[remote_id]] == ["vidAAAAAAAA"]

    fake.yt_write_budget = None
    db.execute(update(Job).where(Job.kind == "sync.run").values(run_after=datetime.now(UTC)))
    db.commit()
    drain(registry)
    assert (
        api.get(f"/playlists/{pl['id']}/sync/runs/{sync_run['id']}", 200)["status"] == "succeeded"
    )
    assert [v for _, v in fake.yt_contents[remote_id]] == ["vidAAAAAAAA", "vidCCCCCCCC"]


def test_soundcloud_is_not_a_sync_target(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    pl = playlist(api, "P")
    resp = api.post(
        f"/playlists/{pl['id']}/sync/targets",
        422,
        json={"platform": "soundcloud", "create_new": True},
    )
    assert "nicht unterstützt" in resp["detail"]


def test_deleting_target_keeps_history_and_remote(api, client, registry, fake) -> None:  # type: ignore[no-untyped-def]
    connect(api, client, fake)
    pl = playlist(api, "P", track(api, "Song 1", sources=[sp_link(1)]))
    tgt = target(api, pl)
    sync(api, registry, pl, tgt, preview_run_id=preview(api, registry, pl, tgt)["id"])
    remote_id = api.get(f"/playlists/{pl['id']}/sync/targets", 200)[0]["remote_playlist_id"]
    api.delete(f"/playlists/{pl['id']}/sync/targets/{tgt['id']}", 204)
    assert api.get(f"/playlists/{pl['id']}/sync/targets", 200) == []
    history = api.get(f"/playlists/{pl['id']}/sync/runs", 200)
    assert history["total"] == 2 and history["items"][0]["target_id"] is None
    assert sp_ids(fake, remote_id) == [sid(1)]
