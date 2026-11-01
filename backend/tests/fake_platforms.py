"""Nachgebildete Plattform-APIs für Tests (httpx.MockTransport).

Die Antworten folgen den offiziellen Referenzen (Stand 2026):
* Spotify: GET /me, GET /me/playlists (Feld ``items.total``), GET /playlists/{id}/items
  (Feld ``item``; ``track`` veraltet), GET /tracks/{id}; Token-Endpunkt accounts.spotify.com.
* YouTube Data API v3: channels.list, playlists.list, playlistItems.list, videos.list;
  Fehlerformat ``error.errors[].reason`` (z. B. ``quotaExceeded``); Google-Token/Revoke.
* SoundCloud: /me, /me/playlists und /playlists/{urn}/tracks mit ``collection``/``next_href``.

Jede Anfrage wird in ``requests`` protokolliert – Tests prüfen damit, dass gegenüber den
Plattform-APIs ausschließlich GET verwendet wird.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs

import httpx

TOKEN_HOSTS = {"accounts.spotify.com", "oauth2.googleapis.com", "secure.soundcloud.com"}
API_HOSTS = {"api.spotify.com", "www.googleapis.com", "api.soundcloud.com"}


def sp_track(n: int, **kw: Any) -> dict[str, Any]:
    tid = f"{n:0>22}"
    data = {
        "id": tid,
        "type": "track",
        "name": f"Song {n}",
        "artists": [{"id": "a1", "name": "Artist", "type": "artist"}],
        "duration_ms": 200_000 + n * 1000,
        "external_ids": {"isrc": f"GBAAA26{n:0>5}"},
        "external_urls": {"spotify": f"https://open.spotify.com/track/{tid}"},
        "album": {"name": "Album", "release_date": "2024-05-17", "release_date_precision": "day"},
        "is_local": False,
        "is_playable": True,
    }
    data.update(kw)
    return data


class FakePlatforms:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        #: Pfad → Antwort oder Funktion(Request) → Antwort; für Fehlerfälle überschreibbar
        self.overrides: dict[str, Callable[[httpx.Request], httpx.Response] | httpx.Response] = {}
        self.token_counter = 0
        self.refresh_fails = False
        self.revoked: list[str] = []
        # Spotify-Daten
        self.sp_playlists = [
            {
                "id": "pl_own",
                "name": "Warm-up",
                "owner": {"id": "silas", "display_name": "Silas"},
                "collaborative": False,
                "snapshot_id": "snap1",
                "description": None,
                "external_urls": {"spotify": "https://open.spotify.com/playlist/pl_own"},
                "items": {"href": "x", "total": 3},
            },
            {
                "id": "pl_foreign",
                "name": "Today's Top Hits",
                "owner": {"id": "spotify", "display_name": "Spotify"},
                "collaborative": False,
                "snapshot_id": "s9",
                "external_urls": {"spotify": "https://open.spotify.com/playlist/pl_foreign"},
                "items": {"href": "x", "total": 50},
            },
        ]
        self.sp_items: list[dict[str, Any]] = [
            {"added_at": "2026-01-01T00:00:00Z", "is_local": False, "item": sp_track(1)},
            # ältere Antworten nutzen noch das veraltete Feld ``track``
            {"added_at": "2026-01-02T00:00:00Z", "is_local": False, "track": sp_track(2)},
            {
                "is_local": True,
                "item": {
                    "id": None,
                    "type": "track",
                    "name": "Bootleg (lokal)",
                    "artists": [{"name": "DJ"}],
                    "duration_ms": 180000,
                    "is_local": True,
                },
            },
            {"added_at": None, "is_local": False, "item": None},
        ]
        # YouTube-Daten
        self.yt_items = [
            ("vidAAAAAAAA", "Kerala (Official Video)", "Bonobo - Topic", "PT3M50S"),
            ("vidBBBBBBBB", "Deleted video", None, None),
            ("vidCCCCCCCC", "Midnight City", "M83VEVO", "PT4M4S"),
        ]
        self.yt_quota_exhausted = False
        # Schreibzugriffe (Synchronisierung)
        self.writes: list[tuple[str, str, Any]] = []
        self.sp_contents: dict[str, list[dict[str, Any]]] = {}
        #: zusätzliche Spotify-Tracks für die Suche (ID → Track)
        self.sp_catalog: dict[str, dict[str, Any]] = {}
        self.yt_contents: dict[str, list[tuple[str, str]]] = {}
        self.yt_catalog: dict[str, tuple[str, str, str]] = {
            vid: (title, channel or "", duration or "")
            for vid, title, channel, duration in self.yt_items
            if duration
        }
        #: verbleibende Kontingent-Einheiten für Schreibzugriffe (None = unbegrenzt)
        self.yt_write_budget: int | None = None
        self.counter = 0
        #: vom Token-Endpunkt gemeldete Scopes (Tests setzen Schreib-Scopes)
        self.token_scope: str | None = None
        # SoundCloud
        self.sc_tracks = [
            {
                "urn": "soundcloud:tracks:1",
                "title": "Strobe",
                "user": {"username": "deadmau5", "urn": "soundcloud:users:9"},
                "metadata_artist": "deadmau5",
                "duration": 634000,
                "permalink_url": "https://soundcloud.com/deadmau5/strobe",
                "access": "playable",
            },
            {
                "urn": "soundcloud:tracks:2",
                "title": "Gesperrt",
                "user": {"username": "x"},
                "duration": 1000,
                "permalink_url": "https://soundcloud.com/x/gesperrt",
                "access": "blocked",
            },
        ]

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def api_requests(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host in API_HOSTS]

    # --- Routing --------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        override = self.overrides.get(path)
        if override is not None:
            return override(request) if callable(override) else override
        host = request.url.host
        if host in TOKEN_HOSTS:
            return self._token(request)
        if host == "api.spotify.com":
            return self._spotify(request)
        if host == "www.googleapis.com":
            return self._youtube(request)
        if host == "api.soundcloud.com":
            return self._soundcloud(request)
        return httpx.Response(404, json={"error": "unbekannter Host"})

    def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path.endswith("/revoke"):
            self.revoked.append(form.get("token", ""))
            return httpx.Response(200)
        if form.get("grant_type") == "refresh_token" and self.refresh_fails:
            return httpx.Response(400, json={"error": "invalid_grant"})
        if form.get("grant_type") == "authorization_code" and form.get("code") != "good-code":
            return httpx.Response(400, json={"error": "invalid_grant"})
        self.token_counter += 1
        body: dict[str, Any] = {
            "access_token": f"access-{self.token_counter}",
            "token_type": "Bearer",
            "expires_in": 3600,
            "scope": self.token_scope
            or form.get("scope", "playlist-read-private playlist-read-collaborative"),
        }
        if form.get("grant_type") == "authorization_code":
            body["refresh_token"] = "refresh-1"
        return httpx.Response(200, json=body)

    def _auth_ok(self, request: httpx.Request) -> bool:
        return request.headers.get("Authorization", "").split(" ")[-1].startswith("access-")

    def _sp_entry(self, uri: str) -> dict[str, Any]:
        tid = uri.rsplit(":", 1)[-1]
        track = self.sp_catalog.get(tid) or (sp_track(int(tid)) if tid.isdigit() else None)
        return {"added_at": "2026-11-01T00:00:00Z", "is_local": False, "item": track}

    def _sp_content(self, pid: str) -> list[dict[str, Any]] | None:
        if pid == "pl_own":
            return self.sp_items
        return self.sp_contents.get(pid)

    def _spotify_write(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.writes.append((request.method, path, body))
        if path == "/me/playlists" and request.method == "POST":
            self.counter += 1
            pid = f"mh{self.counter}"
            self.sp_contents[pid] = []
            playlist = {
                "id": pid,
                "name": body["name"],
                "owner": {"id": "silas", "display_name": "Silas"},
                "collaborative": False,
                "public": body.get("public"),
                "snapshot_id": f"{pid}-0",
                "external_urls": {"spotify": f"https://open.spotify.com/playlist/{pid}"},
                "items": {"href": "x", "total": 0},
            }
            self.sp_playlists.append(playlist)
            return httpx.Response(201, json=playlist)
        if path.startswith("/playlists/") and path.endswith("/items"):
            pid = path.split("/")[2]
            content = self._sp_content(pid)
            if content is None:
                return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
            uris = body.get("uris") or []
            if len(uris) > 100:
                return httpx.Response(400, json={"error": {"status": 400, "message": "Too many"}})
            entries = [self._sp_entry(u) for u in uris]
            if request.method == "POST":
                content.extend(entries)
            elif request.method == "PUT":
                content[:] = entries
            else:
                return httpx.Response(405)
            return httpx.Response(201, json={"snapshot_id": f"{pid}-{len(self.writes)}"})
        return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})

    def _spotify_search(self, q: str) -> httpx.Response:
        catalog = {**{f"{n:0>22}": sp_track(n) for n in range(1, 60)}, **self.sp_catalog}
        if q.startswith("isrc:"):
            isrc = q.removeprefix("isrc:")
            hits = [t for t in catalog.values() if t["external_ids"].get("isrc") == isrc]
        else:
            parts = q.split('"')
            title, artist = parts[1].strip().lower(), parts[3].strip().lower()
            hits = [
                t
                for t in catalog.values()
                if t["name"].lower() == title
                and any(a["name"].lower() == artist for a in t["artists"])
            ]
        return httpx.Response(200, json={"tracks": {"items": hits[:10], "total": len(hits)}})

    def _spotify(self, request: httpx.Request) -> httpx.Response:
        if not self._auth_ok(request):
            return httpx.Response(401, json={"error": {"status": 401, "message": "Invalid"}})
        path = request.url.path.removeprefix("/v1")
        q = request.url.params
        if request.method != "GET":
            return self._spotify_write(request, path)
        if path == "/search":
            return self._spotify_search(q.get("q", ""))
        if path == "/me":
            return httpx.Response(200, json={"id": "silas", "display_name": "Silas"})
        if path == "/me/playlists":
            offset = int(q.get("offset", 0))
            items = self.sp_playlists[offset : offset + 1]
            nxt = (
                f"https://api.spotify.com/v1/me/playlists?offset={offset + 1}&limit=1"
                if offset + 1 < len(self.sp_playlists)
                else None
            )
            return httpx.Response(
                200, json={"items": items, "next": nxt, "total": len(self.sp_playlists)}
            )
        if path.startswith("/playlists/") and path.endswith("/items"):
            pid = path.split("/")[2]
            content = self._sp_content(pid)
            if content is None:
                return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
            offset = int(q.get("offset", 0))
            page = content[offset : offset + 2]
            nxt = (
                f"https://api.spotify.com/v1/playlists/{pid}/items?offset={offset + 2}&limit=2"
                if offset + 2 < len(content)
                else None
            )
            return httpx.Response(
                200,
                json={"items": page, "next": nxt, "total": len(content), "offset": offset},
            )
        if path.startswith("/tracks/"):
            tid = path.split("/")[2]
            if tid == "0" * 21 + "9":
                return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})
            n = int(tid)
            return httpx.Response(200, json=sp_track(n))
        return httpx.Response(404, json={"error": {"status": 404, "message": "Not found"}})

    def _youtube(self, request: httpx.Request) -> httpx.Response:
        if self.yt_quota_exhausted:
            return httpx.Response(
                403,
                json={
                    "error": {
                        "code": 403,
                        "message": "quota",
                        "errors": [{"reason": "quotaExceeded", "domain": "youtube.quota"}],
                    }
                },
            )
        if not self._auth_ok(request):
            return httpx.Response(401, json={"error": {"code": 401, "message": "Invalid"}})
        path = request.url.path.removeprefix("/youtube/v3/")
        q = request.url.params
        if request.method != "GET":
            return self._youtube_write(request, path)
        if path == "search":
            words = q.get("q", "").lower().split()
            hits = [
                {"id": {"kind": "youtube#video", "videoId": vid}}
                for vid, (title, channel, _) in self.yt_catalog.items()
                if all(w in f"{title} {channel}".lower() for w in words)
            ]
            return httpx.Response(200, json={"items": hits[:5]})
        if path == "playlistItems" and q.get("playlistId") in self.yt_contents:
            content = self.yt_contents[q["playlistId"]]
            items = []
            for i, (item_id, vid) in enumerate(content):
                title, channel, _ = self.yt_catalog.get(vid, ("Deleted video", "", ""))
                items.append(
                    {
                        "id": item_id,
                        "snippet": {
                            "title": title,
                            "position": i,
                            "videoOwnerChannelTitle": channel,
                        },
                        "contentDetails": {"videoId": vid},
                        "status": {"privacyStatus": "private"},
                    }
                )
            return httpx.Response(
                200, json={"items": items, "pageInfo": {"totalResults": len(items)}}
            )
        if path == "channels":
            return httpx.Response(
                200, json={"items": [{"id": "UCsilas", "snippet": {"title": "Silas"}}]}
            )
        if path == "playlists":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "PLmix",
                            "etag": "etag-1",
                            "snippet": {"title": "Chill", "channelTitle": "Silas"},
                            "contentDetails": {"itemCount": 3},
                        },
                        *[
                            {
                                "id": pid,
                                "etag": f"e{len(c)}",
                                "snippet": {"title": pid, "channelTitle": "Silas"},
                                "contentDetails": {"itemCount": len(c)},
                            }
                            for pid, c in self.yt_contents.items()
                        ],
                    ],
                    "pageInfo": {"totalResults": 1},
                },
            )
        if path == "playlistItems":
            items = [
                {
                    "id": f"item{i}",
                    "snippet": {
                        "title": title,
                        "position": i,
                        **({"videoOwnerChannelTitle": channel} if channel else {}),
                    },
                    "contentDetails": {"videoId": vid},
                    "status": {"privacyStatus": "public"},
                }
                for i, (vid, title, channel, _) in enumerate(self.yt_items)
            ]
            return httpx.Response(
                200, json={"items": items, "pageInfo": {"totalResults": len(items)}}
            )
        if path == "videos":
            wanted = set(q.get("id", "").split(","))
            items = [
                {
                    "id": vid,
                    "snippet": {"title": title, "channelTitle": channel},
                    "contentDetails": {"duration": duration},
                    "status": {"privacyStatus": "public"},
                }
                for vid, (title, channel, duration) in self.yt_catalog.items()
                if vid in wanted
            ]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not found"}})

    def _quota_error(self) -> httpx.Response:
        return httpx.Response(
            403,
            json={"error": {"code": 403, "errors": [{"reason": "quotaExceeded"}]}},
        )

    def _youtube_write(self, request: httpx.Request, path: str) -> httpx.Response:
        body = json.loads(request.content or b"{}")
        self.writes.append((request.method, path, body))
        if self.yt_write_budget is not None:
            if self.yt_write_budget < 50:
                return self._quota_error()
            self.yt_write_budget -= 50
        q = request.url.params
        if path == "playlists" and request.method == "POST":
            self.counter += 1
            pid = f"PLmh{self.counter}"
            self.yt_contents[pid] = []
            return httpx.Response(
                200, json={"id": pid, "etag": "e", "snippet": body.get("snippet", {})}
            )
        if path == "playlistItems":
            snippet = body.get("snippet") or {}
            if request.method == "DELETE":
                for content in self.yt_contents.values():
                    content[:] = [c for c in content if c[0] != q.get("id")]
                return httpx.Response(204)
            pid = snippet.get("playlistId")
            if pid not in self.yt_contents:
                return httpx.Response(
                    404, json={"error": {"code": 404, "errors": [{"reason": "playlistNotFound"}]}}
                )
            vid = snippet["resourceId"]["videoId"]
            if vid not in self.yt_catalog:
                return httpx.Response(
                    404, json={"error": {"code": 404, "errors": [{"reason": "videoNotFound"}]}}
                )
            content = self.yt_contents[pid]
            if request.method == "POST":
                self.counter += 1
                item_id = f"it{self.counter}"
                content.append((item_id, vid))
                return httpx.Response(200, json={"id": item_id})
            if request.method == "PUT":
                entry = next(c for c in content if c[0] == body["id"])
                content.remove(entry)
                content.insert(snippet["position"], entry)
                return httpx.Response(200, json={"id": body["id"]})
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not found"}})

    def _soundcloud(self, request: httpx.Request) -> httpx.Response:
        if not request.headers.get("Authorization", "").startswith("OAuth access-"):
            return httpx.Response(401, json={"error": "unauthorized"})
        path = request.url.path
        if path == "/me":
            return httpx.Response(200, json={"urn": "soundcloud:users:1", "username": "silas"})
        if path == "/me/playlists":
            return httpx.Response(
                200,
                json={
                    "collection": [
                        {
                            "urn": "soundcloud:playlists:7",
                            "title": "Sets",
                            "track_count": 2,
                            "permalink_url": "https://soundcloud.com/silas/sets/sets",
                            "last_modified": "2026/10/01 10:00:00 +0000",
                            "user": {"urn": "soundcloud:users:1", "username": "silas"},
                        }
                    ]
                },
            )
        if path.endswith("/tracks"):
            return httpx.Response(200, json={"collection": self.sc_tracks})
        return httpx.Response(404, json={"error": "not found"})


def json_body(request: httpx.Request) -> dict[str, Any]:
    return json.loads(request.content or b"{}")
