"""API der Bibliothek: Tracks, Quellen, Tags, Playlists, Statistik."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db

SPOTIFY = "https://open.spotify.com/track/{}"


def sid(n: int) -> str:
    """Gültige, eindeutige Spotify-ID."""
    return f"{n:0>22}"


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


# --- Tracks ----------------------------------------------------------------------


def test_create_track_with_all_fields_tags_and_source(api) -> None:  # type: ignore[no-untyped-def]
    tag = api.post("/tags", 201, json={"name": "House", "category": "Genre"})
    created = track(
        api,
        "Strings of Life",
        "Rhythim Is Rhythim",
        album="Strings of Life",
        version="Original Mix",
        duration_ms=444_000,
        isrc="GB-AAA-87-00001",
        release_date="1987-01-01",
        tag_ids=[tag["id"]],
        sources=[{"url": SPOTIFY.format(sid(1)), "availability": "available"}],
    )
    assert created["version"] == "Original Mix"
    assert created["isrc"] == "GBAAA8700001"
    assert created["release_date"] == "1987-01-01"
    assert [t["name"] for t in created["tags"]] == ["House"]
    assert created["platforms"] == ["spotify"]
    assert created["sources"][0]["external_id"] == sid(1)
    assert created["sources"][0]["last_checked_at"] is not None
    assert created["playlists"] == []


def test_update_track_and_clear_optional_fields(api) -> None:  # type: ignore[no-untyped-def]
    t = track(api, "Song", album="Album", version="Live")
    updated = api.patch(f"/tracks/{t['id']}", 200, json={"album": None, "version": ""})
    assert updated["album"] is None and updated["version"] is None
    assert updated["title"] == "Song"
    resp = api.patch(f"/tracks/{t['id']}", 422, json={"title": None})
    assert resp["code"] == "validation_error"
    api.patch(f"/tracks/{t['id']}", 422, json={"unknown_field": 1})


def test_unknown_tag_ids_are_rejected(api) -> None:  # type: ignore[no-untyped-def]
    resp = api.post(
        "/tracks",
        422,
        json={"title": "x", "artist": "y", "tag_ids": ["00000000-0000-0000-0000-000000000000"]},
    )
    assert resp["code"] == "unprocessable"
    assert resp["missing_tag_ids"]


def test_search_filter_sort_and_paginate(api) -> None:  # type: ignore[no-untyped-def]
    chill = api.post("/tags", 201, json={"name": "Chill"})
    a = track(api, "Alpha", "Zed", duration_ms=100_000, tag_ids=[chill["id"]])
    b = track(api, "Beta", "Yan", duration_ms=300_000, version="Remix")
    c = track(api, "Gamma", "Xi")
    api.post(f"/tracks/{a['id']}/sources", 201, json={"url": SPOTIFY.format(sid(1))})
    api.post(f"/tracks/{b['id']}/sources", 201, json={"url": SPOTIFY.format(sid(2))})
    api.post(f"/tracks/{b['id']}/sources", 201, json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    # unverfügbare Quelle zählt nicht als Plattform
    api.post(
        f"/tracks/{c['id']}/sources",
        201,
        json={"url": "https://soundcloud.com/a/b", "availability": "unavailable"},
    )

    def titles(**params: Any) -> list[str]:
        return [t["title"] for t in api.get("/tracks", 200, params=params)["items"]]

    assert titles() == ["Gamma", "Beta", "Alpha"]  # Standard: nach Artist
    assert titles(sort="title") == ["Alpha", "Beta", "Gamma"]
    assert titles(sort="duration", order="desc") == ["Beta", "Alpha", "Gamma"]  # NULL zuletzt
    assert titles(q="remix") == ["Beta"]
    assert titles(q="beta yan") == ["Beta"]
    assert titles(q="beta zed") == []
    assert titles(platform="spotify") == ["Beta", "Alpha"]
    assert titles(platform=["spotify", "youtube"]) == ["Beta"]
    assert titles(missing_platform="spotify") == ["Gamma"]
    assert titles(platform_count=0) == ["Gamma"]
    assert titles(platform_count=1) == ["Alpha"]
    assert titles(tag=chill["id"]) == ["Alpha"]

    page = api.get("/tracks", 200, params={"sort": "title", "limit": 2, "offset": 2})
    assert page["total"] == 3 and [t["title"] for t in page["items"]] == ["Gamma"]
    api.get("/tracks", 422, params={"sort": "nonsense"})


def test_delete_track_in_playlist_requires_force(api) -> None:  # type: ignore[no-untyped-def]
    t1, t2, t3 = track(api, "1"), track(api, "2"), track(api, "3")
    pl = api.post(
        "/playlists", 201, json={"name": "P", "track_ids": [t1["id"], t2["id"], t3["id"]]}
    )

    conflict = api.delete(f"/tracks/{t2['id']}", 409)
    assert conflict["playlists"][0]["id"] == pl["id"]
    assert api.get(f"/tracks/{t2['id']}", 200)["playlists"] == [
        {"id": pl["id"], "name": "P", "occurrences": 1}
    ]

    api.delete(f"/tracks/{t2['id']}?force=true", 204)
    detail = api.get(f"/playlists/{pl['id']}", 200)
    assert [(i["position"], i["track"]["title"]) for i in detail["items"]] == [(0, "1"), (1, "3")]


# --- Quellen ---------------------------------------------------------------------


def test_source_lifecycle(api) -> None:  # type: ignore[no-untyped-def]
    t = track(api, "Song")
    src = api.post(
        f"/tracks/{t['id']}/sources",
        201,
        json={"platform": "youtube", "external_id": "dQw4w9WgXcQ"},
    )
    assert src["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert src["availability"] == "unknown" and src["last_checked_at"] is None

    updated = api.patch(
        f"/tracks/{t['id']}/sources/{src['id']}", 200, json={"availability": "unavailable"}
    )
    assert updated["availability"] == "unavailable"
    assert updated["last_checked_at"] is not None
    assert api.get(f"/tracks/{t['id']}", 200)["platforms"] == []

    moved = api.patch(
        f"/tracks/{t['id']}/sources/{src['id']}", 200, json={"url": "https://youtu.be/aaaaaaaaaaa"}
    )
    assert moved["external_id"] == "aaaaaaaaaaa"

    api.delete(f"/tracks/{t['id']}/sources/{src['id']}", 204)
    assert api.get(f"/tracks/{t['id']}/sources", 200) == []


def test_source_validation_and_conflicts(api) -> None:  # type: ignore[no-untyped-def]
    a, b = track(api, "A"), track(api, "B")
    url = SPOTIFY.format(sid(7))

    api.post(f"/tracks/{a['id']}/sources", 422, json={})
    assert (
        "unterstützten"
        in api.post(f"/tracks/{a['id']}/sources", 422, json={"url": "https://example.com/x"})[
            "detail"
        ]
    )
    api.post(
        f"/tracks/{a['id']}/sources", 422, json={"platform": "youtube", "url": url}
    )  # Link passt nicht zur Plattform

    api.post(f"/tracks/{a['id']}/sources", 201, json={"url": url})
    dup = api.post(f"/tracks/{a['id']}/sources", 409, json={"url": url})
    assert "bereits zugeordnet" in dup["detail"]
    second = api.post(f"/tracks/{a['id']}/sources", 409, json={"url": SPOTIFY.format(sid(8))})
    assert "bereits eine bestätigte" in second["detail"]
    elsewhere = api.post(f"/tracks/{b['id']}/sources", 409, json={"url": url})
    assert elsewhere["track_id"] == a["id"]
    # als Kandidat darf derselbe Plattform-Track trotzdem vorgemerkt werden
    api.post(f"/tracks/{b['id']}/sources", 201, json={"url": url, "link_status": "candidate"})
    api.get(f"/tracks/{b['id']}/sources/00000000-0000-0000-0000-000000000000", 405)


# --- Tags ------------------------------------------------------------------------


def test_tag_management(api) -> None:  # type: ignore[no-untyped-def]
    house = api.post("/tags", 201, json={"name": "House", "category": "Genre"})
    assert api.post("/tags", 201, json={"name": "Abend"})["category"] == "Allgemein"
    assert (
        "existiert" in api.post("/tags", 409, json={"name": "house", "category": "GENRE"})["detail"]
    )
    t = track(api, "Song")
    api.put(f"/tracks/{t['id']}/tags", 200, json={"tag_ids": [house["id"], house["id"]]})

    listed = {x["name"]: x["track_count"] for x in api.get("/tags", 200)}
    assert listed == {"Abend": 0, "House": 1}
    assert [x["name"] for x in api.get("/tags", 200, params={"category": "genre"})] == ["House"]

    renamed = api.patch(f"/tags/{house['id']}", 200, json={"name": "Deep House"})
    assert renamed["name"] == "Deep House"
    api.delete(f"/tags/{house['id']}", 204)
    assert api.get(f"/tracks/{t['id']}", 200)["tags"] == []


# --- Playlists -------------------------------------------------------------------


def order(detail: dict) -> list[str]:
    positions = [i["position"] for i in detail["items"]]
    assert positions == list(range(len(positions))), "Positionen müssen lückenlos sein"
    return [i["track"]["title"] for i in detail["items"]]


def test_playlist_crud_and_ordering(api) -> None:  # type: ignore[no-untyped-def]
    a, b, c, d = (track(api, x, duration_ms=60_000) for x in "ABCD")
    pl = api.post("/playlists", 201, json={"name": "Abend", "description": "ruhig"})
    assert pl["item_count"] == 0

    detail = api.post(f"/playlists/{pl['id']}/items", 200, json={"track_ids": [a["id"], b["id"]]})
    assert order(detail) == ["A", "B"]
    detail = api.post(
        f"/playlists/{pl['id']}/items", 200, json={"track_ids": [c["id"], d["id"]], "position": 1}
    )
    assert order(detail) == ["A", "C", "D", "B"]
    assert detail["total_duration_ms"] == 240_000

    ids = {i["track"]["title"]: i["id"] for i in detail["items"]}
    assert order(
        api.post(f"/playlists/{pl['id']}/items/{ids['A']}/move", 200, json={"position": 2})
    ) == ["C", "D", "A", "B"]
    assert order(
        api.post(f"/playlists/{pl['id']}/items/{ids['B']}/move", 200, json={"position": 0})
    ) == ["B", "C", "D", "A"]
    # Position über das Ende hinaus = ans Ende
    assert order(
        api.post(f"/playlists/{pl['id']}/items/{ids['C']}/move", 200, json={"position": 99})
    ) == ["B", "D", "A", "C"]

    new_order = [ids[x] for x in "DCBA"]
    assert order(
        api.put(f"/playlists/{pl['id']}/items/order", 200, json={"item_ids": new_order})
    ) == list("DCBA")
    api.put(f"/playlists/{pl['id']}/items/order", 422, json={"item_ids": new_order[:3]})
    api.put(
        f"/playlists/{pl['id']}/items/order", 422, json={"item_ids": [*new_order[:3], new_order[0]]}
    )

    assert order(api.delete(f"/playlists/{pl['id']}/items/{ids['C']}", 200)) == list("DBA")

    renamed = api.patch(f"/playlists/{pl['id']}", 200, json={"name": "Nacht", "description": None})
    assert renamed["name"] == "Nacht" and renamed["description"] is None

    listed = api.get("/playlists", 200)
    assert listed["total"] == 1 and listed["items"][0]["item_count"] == 3

    api.delete(f"/playlists/{pl['id']}", 204)
    api.get(f"/playlists/{pl['id']}", 404)
    assert api.get(f"/tracks/{a['id']}", 200)["playlists"] == []  # Tracks bleiben


def test_playlist_duplicates_and_validation(api) -> None:  # type: ignore[no-untyped-def]
    a = track(api, "A")
    pl = api.post("/playlists", 201, json={"name": "P", "track_ids": [a["id"]]})
    assert order(api.post(f"/playlists/{pl['id']}/items", 200, json={"track_ids": [a["id"]]})) == [
        "A",
        "A",
    ]
    unchanged = api.post(
        f"/playlists/{pl['id']}/items", 200, json={"track_ids": [a["id"]], "skip_existing": True}
    )
    assert len(unchanged["items"]) == 2
    missing = api.post(
        f"/playlists/{pl['id']}/items",
        422,
        json={"track_ids": ["00000000-0000-0000-0000-000000000000"]},
    )
    assert missing["missing_track_ids"]
    api.post("/playlists", 422, json={"name": "  "})
    api.post(
        f"/playlists/{pl['id']}/items/00000000-0000-0000-0000-000000000000/move",
        404,
        json={"position": 0},
    )


# --- Statistik -------------------------------------------------------------------


def test_stats(api) -> None:  # type: ignore[no-untyped-def]
    a, b, _c = track(api, "A"), track(api, "B"), track(api, "C")
    api.post(f"/tracks/{a['id']}/sources", 201, json={"url": SPOTIFY.format(sid(1))})
    api.post(f"/tracks/{b['id']}/sources", 201, json={"url": SPOTIFY.format(sid(2))})
    api.post(f"/tracks/{b['id']}/sources", 201, json={"url": "https://youtu.be/dQw4w9WgXcQ"})
    api.post(
        f"/tracks/{b['id']}/sources",
        201,
        json={"url": "https://soundcloud.com/x/y", "availability": "unavailable"},
    )
    api.post("/playlists", 201, json={"name": "P"})

    stats = api.get("/stats", 200)
    assert stats["tracks"] == 3 and stats["playlists"] == 1
    assert stats["local_only"] == 1
    assert stats["single_platform"] == 1
    by_platform = {p["platform"]: p for p in stats["platforms"]}
    assert by_platform["spotify"] == {
        "platform": "spotify",
        "tracks": 2,
        "exclusive": 1,
        "unavailable": 0,
    }
    assert by_platform["youtube"]["tracks"] == 1 and by_platform["youtube"]["exclusive"] == 0
    assert by_platform["soundcloud"] == {
        "platform": "soundcloud",
        "tracks": 0,
        "exclusive": 0,
        "unavailable": 1,
    }
