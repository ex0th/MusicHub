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
            "scope": form.get("scope", "playlist-read-private playlist-read-collaborative"),
        }
        if form.get("grant_type") == "authorization_code":
            body["refresh_token"] = "refresh-1"
        return httpx.Response(200, json=body)

    def _auth_ok(self, request: httpx.Request) -> bool:
        return request.headers.get("Authorization", "").split(" ")[-1].startswith("access-")

    def _spotify(self, request: httpx.Request) -> httpx.Response:
        if not self._auth_ok(request):
            return httpx.Response(401, json={"error": {"status": 401, "message": "Invalid"}})
        path = request.url.path.removeprefix("/v1")
        q = request.url.params
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
            if pid != "pl_own":
                return httpx.Response(403, json={"error": {"status": 403, "message": "Forbidden"}})
            offset = int(q.get("offset", 0))
            page = self.sp_items[offset : offset + 2]
            nxt = (
                f"https://api.spotify.com/v1/playlists/{pid}/items?offset={offset + 2}&limit=2"
                if offset + 2 < len(self.sp_items)
                else None
            )
            return httpx.Response(
                200,
                json={"items": page, "next": nxt, "total": len(self.sp_items), "offset": offset},
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
                        }
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
                for vid, title, channel, duration in self.yt_items
                if vid in wanted and duration
            ]
            return httpx.Response(200, json={"items": items})
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
