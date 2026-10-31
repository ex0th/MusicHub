"""Import-Assistent und Export über die API – inklusive Wiederholungen und Fehlerfällen."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.db

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "import"


@pytest.fixture
def api(client: TestClient, auth: dict[str, str], db):  # type: ignore[no-untyped-def]
    class Api:
        def call(self, method: str, path: str, expected: int | None = None, **kw: Any) -> Any:
            resp = client.request(method, f"/api/v1{path}", headers=auth, **kw)
            if expected is not None:
                assert resp.status_code == expected, resp.text
            return resp

        def upload(self, name: str, content: bytes | str, expected: int = 201, **form: str) -> Any:
            data = content.encode() if isinstance(content, str) else content
            return self.call(
                "POST", "/imports", expected, files={"file": (name, data, "text/plain")}, data=form
            ).json()

        def example(self, name: str, **form: str) -> Any:
            return self.upload(name, (EXAMPLES / name).read_bytes(), **form)

        def items(self, run_id: str, **params: Any) -> list[dict]:
            return self.call("GET", f"/imports/{run_id}/items", 200, params=params).json()["items"]

        def commit(self, run_id: str) -> dict:
            return self.call("POST", f"/imports/{run_id}/commit", 200).json()

        def get(self, path: str, **params: Any) -> Any:
            return self.call("GET", path, 200, params=params).json()

    return Api()


def import_file(api, name: str, content: bytes | str | None = None, **form: str) -> dict:  # type: ignore[no-untyped-def]
    run = api.example(name, **form) if content is None else api.upload(name, content, **form)
    return api.commit(run["id"])


# --- Analyse und Vorschau -----------------------------------------------------------


def test_upload_analyzes_without_changing_library(api) -> None:  # type: ignore[no-untyped-def]
    run = api.example("tunemymusic.csv")
    assert run["status"] == "analyzed"
    assert run["format"] == "tunemymusic_csv" and run["format_label"] == "TuneMyMusic CSV"
    # Kerala steht in zwei Playlists -> zweites Vorkommen ist ein Datei-Duplikat
    assert run["summary"] == {
        "new": 4,
        "existing": 0,
        "potential_duplicate": 0,
        "file_duplicate": 1,
        "invalid": 0,
        "total": 5,
    }
    assert [(p["name"], p["track_count"], p["mode"]) for p in run["playlists"]] == [
        ("Sonntagabend", 3, "create"),
        ("Lauftraining", 2, "create"),
    ]
    assert api.get("/stats")["tracks"] == 0  # noch nichts geschrieben

    items = api.items(run["id"], status="file_duplicate")
    assert items[0]["title"] == "Kerala" and items[0]["duplicate_of_row"] == 3


def test_commit_creates_tracks_sources_and_playlists(api) -> None:  # type: ignore[no-untyped-def]
    run = import_file(api, "tunemymusic.csv")
    assert run["status"] == "committed"
    assert (run["summary"]["created"], run["summary"]["linked"]) == (4, 1)
    playlists = {p["name"]: p for p in run["summary"]["playlists"]}
    assert playlists["Lauftraining"]["added"] == 2

    lists = {p["name"]: p["id"] for p in api.get("/playlists")["items"]}
    detail = api.get(f"/playlists/{lists['Sonntagabend']}")
    assert [i["track"]["title"] for i in detail["items"]] == [
        "Teardrop",
        "Kerala",
        "An Ending (Ascent)",
    ]
    kerala = detail["items"][1]["track"]
    assert kerala["isrc"] == "GBCFB1600303" and kerala["platforms"] == ["spotify"]
    assert kerala["sources"][0]["match_method"] == "import"


def test_repeated_import_creates_no_duplicates(api) -> None:  # type: ignore[no-untyped-def]
    first = import_file(api, "tunemymusic.csv")
    second_run = api.example("tunemymusic.csv")
    assert any("bereits am" in w for w in second_run["warnings"])
    assert second_run["summary"]["existing"] == 4 and second_run["summary"]["new"] == 0
    assert {p["mode"] for p in second_run["playlists"]} == {"merge"}

    second = api.commit(second_run["id"])
    assert second["summary"]["created"] == 0
    assert all(p["added"] == 0 for p in second["summary"]["playlists"])
    stats = api.get("/stats")
    assert stats["tracks"] == 4 and stats["playlists"] == 2
    assert first["id"] != second["id"]


def test_other_format_matches_existing_tracks(api) -> None:  # type: ignore[no-untyped-def]
    import_file(api, "tunemymusic.csv")
    run = api.example("tracklist.txt")
    by_title = {i["title"]: i for i in api.items(run["id"])}
    # Teardrop über Titel+Artist, Kerala über den Spotify-Link
    assert by_title["Teardrop"]["status"] == "existing"
    assert by_title["Teardrop"]["match_reason"] == "metadata"
    assert by_title["Kerala"]["match_reason"] == "source"
    assert by_title["Gymnopédie No. 1"]["status"] == "new"
    assert run["summary"]["invalid"] == 2
    # TXT ohne Playlist-Angabe: Vorschlag „als Playlist anlegen“ mit Dateinamen
    assert run["unassigned"] == {"count": 4, "enabled": True, "name": "tracklist"}

    done = api.commit(run["id"])
    assert done["summary"]["created"] == 1 and done["summary"]["linked"] == 3
    teardrop = api.get(f"/tracks/{by_title['Teardrop']['match']['id']}")
    assert teardrop["duration_ms"] == 330_000  # leeres Feld wurde ergänzt
    names = [p["name"] for p in api.get("/playlists")["items"]]
    assert "tracklist" in names


def test_potential_duplicates_and_decisions(api) -> None:  # type: ignore[no-untyped-def]
    import_file(api, "x.csv", "title,artist,duration\nBlinding Lights,The Weeknd,3:20\n")
    run = api.upload(
        "y.csv",
        "title,artist,duration\nBlinding Light,The Weeknd,3:21\nBlinding Lights,The Weeknd,3:20\n",
    )
    potential, existing = api.items(run["id"])
    assert potential["status"] == "potential_duplicate"
    assert potential["match"]["title"] == "Blinding Lights" and potential["match_score"] >= 0.75
    assert existing["status"] == "existing"

    changed = api.call(
        "PATCH", f"/imports/{run['id']}/items/{potential['id']}", 200, json={"decision": "create"}
    ).json()
    assert changed["decision"] == "create"
    api.call(
        "PATCH",
        f"/imports/{run['id']}/items/{potential['id']}",
        422,
        json={"decision": "use_existing", "match_track_id": "00000000-0000-0000-0000-000000000000"},
    )
    done = api.commit(run["id"])
    assert done["summary"]["created"] == 1
    assert api.get("/stats")["tracks"] == 2


def test_invalid_rows_are_reported_not_fatal(api) -> None:  # type: ignore[no-untyped-def]
    run = api.example("bibliothek.csv")
    assert run["summary"]["invalid"] == 1
    invalid = api.items(run["id"], status="invalid")[0]
    assert invalid["row"] == 5 and invalid["errors"] == ["Titel fehlt."]
    api.call(
        "PATCH", f"/imports/{run['id']}/items/{invalid['id']}", 422, json={"decision": "create"}
    )
    done = api.commit(run["id"])
    assert done["summary"]["created"] == 4 and done["summary"]["skipped"] == 1
    spastik = api.get("/tracks", q="spastik")["items"][0]
    assert spastik["version"] == "Dubfire Remix"
    assert [t["name"] for t in spastik["tags"]] == ["Techno"]


def test_playlist_modes(api) -> None:  # type: ignore[no-untyped-def]
    csv_v1 = "title,artist,playlist\nA,X,Mix\nB,X,Mix\n"
    import_file(api, "v1.csv", csv_v1)
    run = api.upload("v2.csv", "title,artist,playlist\nC,X,Mix\nA,X,Mix\n")
    assert run["playlists"][0]["mode"] == "merge"
    api.call(
        "PATCH",
        f"/imports/{run['id']}",
        422,
        json={"playlists": [{"name": "Gibt es nicht", "mode": "skip"}]},
    )
    api.call(
        "PATCH",
        f"/imports/{run['id']}",
        200,
        json={"playlists": [{"name": "Mix", "mode": "replace"}]},
    )
    api.commit(run["id"])
    mix = api.get("/playlists")["items"][0]
    detail = api.get(f"/playlists/{mix['id']}")
    assert [i["track"]["title"] for i in detail["items"]] == ["C", "A"]
    assert api.get("/stats")["tracks"] == 3  # B bleibt in der Bibliothek


def test_commit_only_once_and_discard(api) -> None:  # type: ignore[no-untyped-def]
    run = api.upload("x.csv", "title,artist\nA,B\n")
    api.commit(run["id"])
    assert (
        api.call("POST", f"/imports/{run['id']}/commit", 409).json()["import_status"] == "committed"
    )
    other = api.upload("y.csv", "title,artist\nC,D\n")
    assert api.call("POST", f"/imports/{other['id']}/discard", 200).json()["status"] == "discarded"
    api.call("POST", f"/imports/{other['id']}/commit", 409)
    assert api.get("/stats")["tracks"] == 1


@pytest.mark.parametrize(
    ("name", "content", "message"),
    [
        ("evil.exe", b"MZ", "nicht unterstützt"),
        ("x.csv", b"foo,bar\n1,2\n", "Spalten nicht erkannt"),
        ("x.json", b"{kaputt", "Ungültiges JSON"),
        ("x.csv", b"title,artist\n\x00\x00", "Binärdaten"),
    ],
)
def test_rejected_uploads_are_logged(api, name: str, content: bytes, message: str) -> None:  # type: ignore[no-untyped-def]
    problem = api.upload(name, content, expected=422)
    assert message in problem["detail"]
    log = api.get("/imports")["items"][0]
    assert log["id"] == problem["import_id"] and log["status"] == "failed"
    assert message in log["error"]


def test_upload_size_limit(api, app) -> None:  # type: ignore[no-untyped-def]
    original = app.state.settings.import_max_bytes
    app.state.settings.import_max_bytes = 2048
    try:
        problem = api.upload("big.csv", "title,artist\n" + "A,B\n" * 2000, expected=413)
        assert problem["code"] == "payload_too_large"
        assert problem["detail"] == "Die Datei ist größer als 2 KB."
        assert api.get("/imports")["items"][0]["status"] == "failed"
    finally:
        app.state.settings.import_max_bytes = original


def test_import_report_download(api) -> None:  # type: ignore[no-untyped-def]
    run = import_file(api, "tracklist.txt")
    resp = api.call("GET", f"/imports/{run['id']}/report", 200)
    assert resp.headers["content-disposition"].startswith("attachment;")
    rows = list(csv.DictReader(io.StringIO(resp.content.decode("utf-8-sig"))))
    assert len(rows) == 6
    assert {r["status"] for r in rows} == {"new", "invalid"}
    assert any("Artist nicht erkennbar" in r["fehler"] for r in rows)
    report = api.call("GET", f"/imports/{run['id']}/report", 200, params={"format": "json"}).json()
    assert report["import"]["status"] == "committed" and len(report["items"]) == 6


def test_import_requires_write_permission(client: TestClient, db) -> None:  # type: ignore[no-untyped-def]
    resp = client.post("/api/v1/imports", files={"file": ("x.csv", b"title,artist\nA,B\n")})
    assert resp.status_code == 401


# --- Export ---------------------------------------------------------------------------


def _seed(api) -> None:  # type: ignore[no-untyped-def]
    tag = api.call("POST", "/tags", 201, json={"name": "House", "category": "Genre"}).json()
    api.call(
        "POST",
        "/tracks",
        201,
        json={
            "title": '=HYPERLINK("http://evil")',
            "artist": "-Minus",
            "version": "@Mix",
            "duration_ms": 200_000,
            "release_date": "2020-02-02",
            "tag_ids": [tag["id"]],
            "sources": [{"url": "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"}],
        },
    )
    import_file(api, "tunemymusic.csv")


def test_csv_export_prevents_formula_injection(api) -> None:  # type: ignore[no-untyped-def]
    _seed(api)
    resp = api.call("GET", "/export/library", 200, params={"format": "csv"})
    assert resp.headers["content-type"].startswith("text/csv")
    assert 'filename="musichub-bibliothek-' in resp.headers["content-disposition"]
    text = resp.content.decode("utf-8")
    assert text.startswith("﻿musichub_id,title,artist")
    rows = list(csv.DictReader(io.StringIO(text.lstrip("﻿"))))
    evil = next(r for r in rows if "HYPERLINK" in r["title"])
    assert (
        evil["title"].startswith("'=")
        and evil["artist"] == "'-Minus"
        and evil["version"] == "'@Mix"
    )
    assert evil["tags"] == "Genre:House" and evil["playlist"] == ""
    assert evil["spotify_url"] == "https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT"
    # Tracks in Playlists: eine Zeile je Eintrag, Kerala steht in zwei Playlists
    assert sum(r["title"] == "Kerala" for r in rows) == 2
    assert all(not r[c].startswith(("=", "+", "-", "@")) for r in rows for c in r if r[c])


@pytest.mark.parametrize("fmt", ["csv", "json"])
def test_export_roundtrip_without_duplicates(api, fmt: str) -> None:  # type: ignore[no-untyped-def]
    _seed(api)
    before = api.get("/stats")
    exported = api.call("GET", "/export/library", 200, params={"format": fmt}).content

    run = api.upload(f"export.{fmt}", exported)
    assert run["format"] == f"musichub_{fmt}"
    assert run["summary"]["new"] == 0 and run["summary"]["invalid"] == 0
    reasons = {i["match_reason"] for i in api.items(run["id"]) if i["status"] == "existing"}
    assert reasons == {"musichub_id"}
    api.commit(run["id"])
    after = api.get("/stats")
    assert (after["tracks"], after["playlists"], after["tags"]) == (
        before["tracks"],
        before["playlists"],
        before["tags"],
    )
    evil = api.get("/tracks", q="HYPERLINK")["items"][0]
    assert evil["title"] == '=HYPERLINK("http://evil")'  # Schutzzeichen wieder entfernt


def test_export_into_empty_library_restores_everything(api, db) -> None:  # type: ignore[no-untyped-def]
    from sqlalchemy import text

    from musichub.db.session import get_engine

    _seed(api)
    exported = api.call("GET", "/export/library", 200, params={"format": "json"}).content
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE playlist_items, track_sources, track_tags, tags, playlists, tracks CASCADE"
            )
        )

    run = api.upload("backup.json", exported)
    assert run["summary"]["new"] == 5
    api.commit(run["id"])
    stats = api.get("/stats")
    assert (stats["tracks"], stats["playlists"], stats["tags"]) == (5, 2, 1)
    lists = {p["name"]: p["id"] for p in api.get("/playlists")["items"]}
    order = [i["track"]["title"] for i in api.get(f"/playlists/{lists['Sonntagabend']}")["items"]]
    assert order == ["Teardrop", "Kerala", "An Ending (Ascent)"]


def test_playlist_export(api) -> None:  # type: ignore[no-untyped-def]
    import_file(api, "tunemymusic.csv")
    playlist = next(p for p in api.get("/playlists")["items"] if p["name"] == "Lauftraining")
    data = api.call("GET", f"/playlists/{playlist['id']}/export", 200, params={"format": "json"})
    assert 'filename="playlist-lauftraining-' in data.headers["content-disposition"]
    doc = json.loads(data.content)
    assert doc["scope"] == "playlist" and len(doc["tracks"]) == 2
    assert [t["title"] for t in doc["tracks"] if t["id"] in doc["playlists"][0]["tracks"]]
    rows = list(
        csv.DictReader(
            io.StringIO(
                api.call(
                    "GET", f"/playlists/{playlist['id']}/export", 200, params={"format": "csv"}
                ).content.decode("utf-8-sig")
            )
        )
    )
    assert [(r["title"], r["playlist"], r["playlist_position"]) for r in rows] == [
        ("Innerbloom", "Lauftraining", "1"),
        ("Kerala", "Lauftraining", "2"),
    ]
    api.call("GET", "/playlists/00000000-0000-0000-0000-000000000000/export", 404)
