"""Matching über die API: Scan, Entscheidungen, Zusammenführen, Rückgängig, Automatik."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from musichub.models import Job

pytestmark = pytest.mark.db

SPOTIFY = "https://open.spotify.com/track/{}"
YOUTUBE = "https://www.youtube.com/watch?v={}"


def sid(n: int) -> str:
    return f"{n:0>22}"


def yid(n: int) -> str:
    return f"vid{n:0>8}"


@pytest.fixture
def api(client: TestClient, auth: dict[str, str], db):  # type: ignore[no-untyped-def]
    class Api:
        def __getattr__(self, method: str):  # type: ignore[no-untyped-def]
            def call(path: str, expected: int | None = None, **kw: Any) -> Any:
                resp = client.request(method.upper(), f"/api/v1{path}", headers=auth, **kw)
                if expected is not None:
                    assert resp.status_code == expected, resp.text
                return resp.json() if resp.content else None

            return call

    return Api()


def track(api, title: str, artist: str = "Artist", **kw: Any) -> dict:  # type: ignore[no-untyped-def]
    return api.post("/tracks", 201, json={"title": title, "artist": artist, **kw})


def candidates(api, **params: Any) -> list[dict]:  # type: ignore[no-untyped-def]
    return api.get("/matching/candidates", 200, params={"limit": 100, **params})["items"]


def pair(c: dict) -> set[str]:
    return {c["source"]["id"], c["suggested"]["id"]}


# --- Scan ------------------------------------------------------------------------------------


def test_scan_suggests_duplicates_but_not_other_versions(api) -> None:  # type: ignore[no-untyped-def]
    sp = track(api, "Track Name (Original Mix)", duration_ms=312_400)
    sc = track(api, "Track Name", duration_ms=313_000)
    yt = track(api, "Artist - Track Name [Official Audio]", "Artist - Topic", duration_ms=312_900)
    vip = track(api, "Track Name (VIP Mix)", duration_ms=312_400)
    remix = track(api, "Track Name (Someone Remix)", duration_ms=312_400)
    track(api, "Track 01", "Unknown Artist")
    track(api, "Track 01", "Unknown Artist")
    track(api, "Kerala", "Bonobo", duration_ms=230_000)
    track(api, "Kerala", "Bonobos", duration_ms=230_000)

    result = api.post("/matching/scan", 200)
    assert result["tracks"] == 9
    assert result["unmatchable"] == 2

    found = candidates(api)
    pairs = [pair(c) for c in found]
    for a, b in ((sp, sc), (sp, yt), (sc, yt)):
        assert {a["id"], b["id"]} in pairs
    for other in (vip, remix):
        assert all(other["id"] not in p for p in pairs), "andere Version vorgeschlagen"
    certain = [c for c in found if c["level"] == "certain"]
    assert len(certain) == 3
    review = [c for c in found if c["level"] == "review"]
    assert len(review) == 1 and review[0]["source"]["artist"].startswith("Bonobo")
    assert review[0]["blockers"] == ["artist_similar_name"]
    assert not review[0]["auto_merge_allowed"]
    assert all(c["reasons"] and c["source"]["playlist_count"] == 0 for c in found)

    summary = api.get("/matching/summary", 200)
    assert summary["statuses"]["open"] == 4
    assert summary["open_levels"] == {"certain": 3, "likely": 0, "review": 1}
    assert summary["auto_merge_ready"] == 3

    # Erneuter Scan erzeugt nichts doppelt
    again = api.post("/matching/scan", 200)
    assert again["created"] == 0 and again["updated"] == 0
    assert len(candidates(api)) == 4


def test_scan_removes_suggestion_when_track_changes(api) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "Song", duration_ms=200_000)
    b = track(api, "Song", duration_ms=200_500)
    api.post("/matching/scan", 200)
    assert len(candidates(api)) == 1
    api.patch(f"/tracks/{b['id']}", 200, json={"version": "Live"})
    result = api.post("/matching/scan", 200)
    assert result["removed"] == 1
    assert candidates(api) == []
    assert a


# --- Entscheidungen --------------------------------------------------------------------------


def test_reject_is_remembered_and_can_be_corrected(api) -> None:  # type: ignore[no-untyped-def]
    track(api, "Song", duration_ms=200_000)
    track(api, "Song", duration_ms=200_000)
    api.post("/matching/scan", 200)
    (cand,) = candidates(api)

    rejected = api.post(f"/matching/candidates/{cand['id']}/reject", 200)
    assert rejected["status"] == "rejected" and rejected["decided_by"]
    api.post("/matching/scan", 200)
    assert candidates(api) == []
    assert len(candidates(api, status="rejected")) == 1
    # Abgelehnt: weder bestätigen noch automatisch zusammenführen
    api.post(f"/matching/candidates/{cand['id']}/confirm", 409, json={})
    assert api.post("/matching/auto-merge", 200)["merged"] == []
    api.post(f"/matching/candidates/{cand['id']}/defer", 409)

    reopened = api.post(f"/matching/candidates/{cand['id']}/reopen", 200)
    assert reopened["status"] == "open"
    assert len(candidates(api)) == 1


def test_defer_moves_suggestion_to_later(api) -> None:  # type: ignore[no-untyped-def]
    track(api, "Song", duration_ms=200_000)
    track(api, "Song", duration_ms=209_000)
    api.post("/matching/scan", 200)
    (cand,) = candidates(api)
    assert cand["level"] == "likely"
    api.post(f"/matching/candidates/{cand['id']}/defer", 200)
    assert candidates(api) == []
    (later,) = candidates(api, status="deferred")
    assert later["id"] == cand["id"]
    # Scan behält den Status bei
    api.post("/matching/scan", 200)
    assert candidates(api, status="deferred")[0]["status"] == "deferred"


# --- Zusammenführen und Rückgängig -----------------------------------------------------------


def test_confirm_merges_everything_and_undo_restores_it(api) -> None:  # type: ignore[no-untyped-def]
    house = api.post("/tags", 201, json={"name": "House"})
    fav = api.post("/tags", 201, json={"name": "Favorit"})
    old = track(
        api,
        "Strobe",
        "deadmau5",
        duration_ms=634_000,
        tag_ids=[house["id"]],
        sources=[{"url": SPOTIFY.format(sid(1))}],
    )
    new = track(
        api,
        "deadmau5 - Strobe (Official Audio)",
        "deadmau5",
        duration_ms=634_500,
        isrc="USUS10900001",
        album="For Lack of a Better Name",
        tag_ids=[house["id"], fav["id"]],
        sources=[{"url": YOUTUBE.format(yid(1)), "availability": "available"}],
    )
    other = track(api, "Ghosts n Stuff", "deadmau5")
    pl = api.post(
        "/playlists",
        201,
        json={"name": "Set", "track_ids": [new["id"], other["id"], old["id"], new["id"]]},
    )
    before = api.get(f"/playlists/{pl['id']}", 200)

    api.post("/matching/scan", 200)
    (cand,) = candidates(api)
    assert cand["suggested"]["id"] == old["id"] and cand["source"]["id"] == new["id"]
    assert cand["source"]["playlist_count"] == 1

    merge = api.post(f"/matching/candidates/{cand['id']}/confirm", 200, json={})
    assert merge["kept_track_id"] == old["id"] and merge["removed_track_id"] == new["id"]
    assert merge["changes"] == {
        "sources": 1,
        "sources_demoted": 0,
        "playlist_items": 2,
        "tags_added": 1,
        "import_items": 0,
    }
    assert merge["filled_fields"] == ["album", "isrc"]

    kept = api.get(f"/tracks/{old['id']}", 200)
    assert kept["title"] == "Strobe"  # lokal führt: Titel bleibt
    assert kept["isrc"] == "USUS10900001" and kept["album"] == "For Lack of a Better Name"
    assert kept["platforms"] == ["spotify", "youtube"]
    assert {t["name"] for t in kept["tags"]} == {"House", "Favorit"}
    api.get(f"/tracks/{new['id']}", 404)
    after = api.get(f"/playlists/{pl['id']}", 200)
    assert [i["position"] for i in after["items"]] == [0, 1, 2, 3]
    assert [i["id"] for i in after["items"]] == [i["id"] for i in before["items"]]
    assert [i["track"]["id"] for i in after["items"]] == [
        old["id"],
        other["id"],
        old["id"],
        old["id"],
    ]
    assert candidates(api) == []

    history = api.get("/matching/merges", 200)
    assert history["total"] == 1 and history["items"][0]["status"] == "merged"

    # Rückgängig: alles wie vorher
    undone = api.post(f"/matching/merges/{merge['id']}/undo", 200)
    assert undone["status"] == "undone" and undone["undone_by"]
    restored = api.get(f"/tracks/{new['id']}", 200)
    assert restored["title"] == "deadmau5 - Strobe (Official Audio)"
    assert restored["isrc"] == "USUS10900001"
    assert restored["platforms"] == ["youtube"]
    assert restored["sources"][0]["availability"] == "available"
    assert {t["name"] for t in restored["tags"]} == {"House", "Favorit"}
    kept = api.get(f"/tracks/{old['id']}", 200)
    assert kept["isrc"] is None and kept["album"] is None
    assert kept["platforms"] == ["spotify"]
    assert {t["name"] for t in kept["tags"]} == {"House"}

    def entries(p: dict) -> list[tuple[str, int, str]]:
        return [(i["id"], i["position"], i["track"]["id"]) for i in p["items"]]

    assert entries(api.get(f"/playlists/{pl['id']}", 200)) == entries(before)

    # Paar gilt danach als unterschiedlich und wird nicht erneut vorgeschlagen
    api.post("/matching/scan", 200)
    assert candidates(api) == []
    (rejected,) = candidates(api, status="rejected")
    assert any(r["code"] == "user_note" for r in rejected["reasons"])
    api.post(f"/matching/merges/{merge['id']}/undo", 409)


def test_merge_with_platform_conflict_keeps_both_links(api) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "Song", duration_ms=200_000, sources=[{"url": SPOTIFY.format(sid(1))}])
    b = track(api, "Song", duration_ms=200_000, sources=[{"url": SPOTIFY.format(sid(2))}])
    api.post("/matching/scan", 200)
    (cand,) = candidates(api)
    assert cand["level"] == "likely"
    assert "platform_conflict" in cand["blockers"]
    assert api.post("/matching/auto-merge", 200)["merged"] == []

    merge = api.post(f"/matching/candidates/{cand['id']}/confirm", 200, json={"keep": "source"})
    assert merge["kept_track_id"] == b["id"]
    assert merge["changes"]["sources_demoted"] == 1
    kept = api.get(f"/tracks/{b['id']}", 200)
    status = {s["external_id"]: s["link_status"] for s in kept["sources"]}
    assert status == {sid(2): "linked", sid(1): "candidate"}

    api.post(f"/matching/merges/{merge['id']}/undo", 200)
    restored = api.get(f"/tracks/{a['id']}", 200)
    assert [(s["external_id"], s["link_status"]) for s in restored["sources"]] == [
        (sid(1), "linked")
    ]


def test_manual_merge_and_compare(api) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "Teardrop", "Massive Attack", duration_ms=330_000)
    b = track(api, "Tear Drop", "Massive Attack", duration_ms=331_000)
    remix = track(api, "Teardrop (Mad Professor Remix)", "Massive Attack")

    cmp = api.get("/matching/compare", 200, params={"track_a": a["id"], "track_b": remix["id"]})
    assert cmp["level"] == "different"
    assert cmp["version_b"].startswith("Remix")
    assert cmp["candidate_id"] is None
    assert any(r["kind"] == "cap" for r in cmp["reasons"])

    api.post("/matching/merge", 422, json={"keep_track_id": a["id"], "merge_track_id": a["id"]})
    api.post(
        "/matching/merge",
        404,
        json={"keep_track_id": a["id"], "merge_track_id": "00000000-0000-0000-0000-000000000000"},
    )
    # Als unterschiedlich markiert -> manuelles Zusammenführen gesperrt
    api.post("/matching/reject-pair", 200, json={"track_a_id": a["id"], "track_b_id": b["id"]})
    api.post("/matching/merge", 409, json={"keep_track_id": a["id"], "merge_track_id": b["id"]})
    cmp = api.get("/matching/compare", 200, params={"track_a": a["id"], "track_b": b["id"]})
    assert cmp["candidate_status"] == "rejected"
    api.post(f"/matching/candidates/{cmp['candidate_id']}/reopen", 200)

    merge = api.post(
        "/matching/merge", 200, json={"keep_track_id": a["id"], "merge_track_id": b["id"]}
    )
    assert merge["method"] == "manual" and merge["level"] in {"certain", "likely"}
    api.get(f"/tracks/{b['id']}", 404)


def test_undo_refuses_when_kept_track_is_gone(api) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "Song", duration_ms=200_000)
    b = track(api, "Song", duration_ms=200_000)
    c = track(api, "Song", duration_ms=200_000)
    first = api.post(
        "/matching/merge", 200, json={"keep_track_id": a["id"], "merge_track_id": b["id"]}
    )
    second = api.post(
        "/matching/merge", 200, json={"keep_track_id": c["id"], "merge_track_id": a["id"]}
    )
    # a ist inzwischen in c aufgegangen -> erst die spätere Zusammenführung zurücknehmen
    resp = api.post(f"/matching/merges/{first['id']}/undo", 409)
    assert resp["track_id"] == a["id"]
    api.post(f"/matching/merges/{second['id']}/undo", 200)
    api.post(f"/matching/merges/{first['id']}/undo", 200)
    for t in (a, b, c):
        api.get(f"/tracks/{t['id']}", 200)


def test_rejections_carry_over_to_kept_track(api) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "Song", duration_ms=200_000)
    b = track(api, "Song", duration_ms=200_000)
    c = track(api, "Song", duration_ms=200_000)
    api.post("/matching/reject-pair", 200, json={"track_a_id": b["id"], "track_b_id": c["id"]})
    merge = api.post(
        "/matching/merge", 200, json={"keep_track_id": a["id"], "merge_track_id": b["id"]}
    )
    # b ≠ c und a = b  =>  a ≠ c
    api.post("/matching/merge", 409, json={"keep_track_id": a["id"], "merge_track_id": c["id"]})
    api.post(f"/matching/merges/{merge['id']}/undo", 200)
    cmp = api.get("/matching/compare", 200, params={"track_a": b["id"], "track_b": c["id"]})
    assert cmp["candidate_status"] == "rejected"
    cmp = api.get("/matching/compare", 200, params={"track_a": a["id"], "track_b": c["id"]})
    assert cmp["candidate_status"] is None


# --- Automatik -------------------------------------------------------------------------------


def test_auto_merge_only_merges_certain_conflict_free_pairs(api, db) -> None:  # type: ignore[no-untyped-def]
    certain_a = track(
        api,
        "Innerbloom",
        "RÜFÜS DU SOL",
        duration_ms=576_000,
        sources=[{"url": SPOTIFY.format(sid(1))}],
    )
    certain_b = track(
        api,
        "Innerbloom",
        "Rufus Du Sol",
        duration_ms=577_000,
        sources=[{"url": YOUTUBE.format(yid(1))}],
    )
    likely_a = track(api, "Song", duration_ms=200_000)
    likely_b = track(api, "Song", duration_ms=207_000)
    track(api, "Strobe", "deadmau5", duration_ms=600_000)
    track(api, "Strobe (Kaskade Remix)", "deadmau5", duration_ms=600_000)
    track(api, "Wonderwall", "Oasis", duration_ms=258_000)
    track(api, "Wonderwall - Live", "Oasis", duration_ms=258_000)
    track(api, "Remastered", "Queen", duration_ms=200_000)
    track(api, "Remastered - Remastered 2011", "Queen", duration_ms=200_000)
    plat_a = track(api, "Twin", duration_ms=180_000, sources=[{"url": SPOTIFY.format(sid(5))}])
    plat_b = track(api, "Twin", duration_ms=180_000, sources=[{"url": SPOTIFY.format(sid(6))}])
    total = api.get("/tracks", 200, params={"limit": 1})["total"]

    api.post("/matching/scan", 200)
    result = api.post("/matching/auto-merge", 200)
    assert len(result["merged"]) == 1
    merged = result["merged"][0]
    assert merged["method"] == "auto" and merged["level"] == "certain"
    assert {merged["kept_track_id"], merged["removed_track_id"]} == {
        certain_a["id"],
        certain_b["id"],
    }
    assert api.get("/tracks", 200, params={"limit": 1})["total"] == total - 1
    for t in (likely_a, likely_b, plat_a, plat_b):
        api.get(f"/tracks/{t['id']}", 200)
    remaining = {c["level"] for c in candidates(api)}
    assert "certain" not in remaining
    # Zweiter Lauf ändert nichts mehr
    assert api.post("/matching/auto-merge", 200)["merged"] == []


def test_auto_merge_rechecks_current_data(api, db) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "Song", duration_ms=200_000)
    b = track(api, "Song", duration_ms=200_000)
    api.post("/matching/scan", 200)
    assert candidates(api)[0]["level"] == "certain"
    # Nach dem Scan geändert: jetzt ein Remix -> darf nicht mehr automatisch laufen
    api.patch(f"/tracks/{b['id']}", 200, json={"version": "Kaskade Remix"})
    result = api.post("/matching/auto-merge", 200)
    assert result["merged"] == [] and result["skipped"] == 1
    api.get(f"/tracks/{a['id']}", 200)
    api.get(f"/tracks/{b['id']}", 200)


# --- Import und Job ----------------------------------------------------------------------------


def test_import_commit_schedules_scan_job(api, client, auth, db) -> None:  # type: ignore[no-untyped-def]
    from musichub.connectors.registry import ConnectorRegistry
    from musichub.jobs.handlers import HANDLERS, JobContext

    track(api, "Teardrop", "Massive Attack", duration_ms=330_000)
    csv = "Track name,Artist name\nTeardropp (Official Video),Massive Attack\n"
    run = client.post(
        "/api/v1/imports", headers=auth, files={"file": ("x.csv", csv.encode(), "text/csv")}
    ).json()
    # Mögliches Duplikat -> als neuen Track anlegen, damit Matching etwas zu tun hat
    items = api.get(f"/imports/{run['id']}/items", 200)["items"]
    assert items[0]["status"] == "potential_duplicate"
    api.patch(f"/imports/{run['id']}/items/{items[0]['id']}", 200, json={"decision": "create"})
    api.post(f"/imports/{run['id']}/commit", 200)
    api.post(f"/imports/{run['id']}/commit", 409)
    jobs = db.scalars(select(Job).where(Job.kind == "matching.scan")).all()
    assert len(jobs) == 1

    assert api.get("/tracks", 200)["total"] == 2
    HANDLERS["matching.scan"](JobContext(db, ConnectorRegistry([]), {}))
    db.commit()
    assert db.execute(text("SELECT count(*) FROM match_candidates")).scalar_one() == 1
