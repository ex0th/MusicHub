"""YouTube Data API v3 – lesend (eigene Playlists und deren Videos).

* OAuth über Google mit dem einzigen Scope ``youtube.readonly``.
* Kontingent: 10 000 Einheiten pro Tag und Projekt; ``playlists.list``,
  ``playlistItems.list``, ``videos.list`` und ``channels.list`` kosten je 1 Einheit (50
  Einträge pro Aufruf). Erschöpftes Kontingent → Job wartet bis nach Mitternacht (PT).
* **Keine YouTube-Music-API:** Die Data API kennt nur YouTube-Playlists des Kontos. In
  YouTube Music angelegte Playlists erscheinen dort in der Regel ebenfalls; die
  YouTube-Music-Mediathek (Alben, Künstler, „Mag ich“-Musik, hochgeladene Musik) ist nicht
  zugänglich. „Später ansehen“ und Verlauf liefert die API nicht.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from musichub.connectors.base import (
    Capability,
    ConnectorAccessError,
    OAuthTokens,
    PlatformConnector,
    RemoteAccount,
    RemotePlaylistInfo,
    RemoteTrack,
    TrackPage,
    TrackQuery,
)
from musichub.connectors.http import ApiClient, OAuthClient
from musichub.connectors.oauth import tokens_from_payload
from musichub.core.config import Settings
from musichub.domain.types import Platform

AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_URL = "https://www.googleapis.com/youtube/v3"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
WRITE_SCOPE = "https://www.googleapis.com/auth/youtube"
PAGE_SIZE = 50

_DURATION = re.compile(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")
_UNAVAILABLE_TITLES = {"Deleted video", "Private video"}
#: Kontingent wird um Mitternacht pazifischer Zeit zurückgesetzt; UTC−8 ist im Sommer
#: eine Stunde nach dem Zurücksetzen – also nie zu früh.
_PACIFIC = timedelta(hours=-8)


def parse_iso_duration(value: str | None) -> int | None:
    match = _DURATION.match(value or "")
    if not match or not any(match.groups()):
        return None
    days, hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    total = ((days * 24 + hours) * 60 + minutes) * 60 + seconds
    return total * 1000 or None


def seconds_until_quota_reset(now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    local = now + _PACIFIC
    next_midnight = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return (next_midnight - local).total_seconds() + 120


def _quota_check(response: httpx.Response) -> float | None:
    try:
        errors = response.json()["error"]["errors"]
        reasons = {e.get("reason") for e in errors if isinstance(e, dict)}
    except (ValueError, KeyError, TypeError):
        return None
    if reasons & {"quotaExceeded", "dailyLimitExceeded"}:
        return seconds_until_quota_reset()
    if reasons & {"rateLimitExceeded", "userRateLimitExceeded"}:
        return 60.0
    return None


def channel_artist(channel: str | None) -> str | None:
    """„Artist - Topic“ (automatisch erzeugte Musikkanäle) → „Artist“."""
    if not channel:
        return None
    return re.sub(r"\s+-\s+Topic$", "", channel.strip()) or None


class YouTubeConnector(PlatformConnector):
    platform = Platform.YOUTUBE
    capabilities = frozenset(
        {
            Capability.OAUTH,
            Capability.READ_PLAYLISTS,
            Capability.REFRESH_METADATA,
            Capability.SEARCH,
            Capability.WRITE_PLAYLISTS,
        }
    )
    scopes = (SCOPE,)
    #: Schreiben von Playlists verlangt laut API ``youtube`` (oder ``youtube.force-ssl``)
    write_scopes = (WRITE_SCOPE,)
    write_cost_units = 50

    def has_write_scope(self, granted: str | None) -> bool:
        have = set((granted or "").split())
        return bool(have & {WRITE_SCOPE, "https://www.googleapis.com/auth/youtube.force-ssl"})

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client_id = settings.youtube_client_id
        secret = settings.youtube_client_secret
        self._client_secret = secret.get_secret_value() if secret else None
        self._api = ApiClient(
            self.platform,
            API_URL,
            timeout=settings.connector_timeout_seconds,
            transport=transport,
            quota_check=_quota_check,
        )
        self._oauth = OAuthClient(
            self.platform, timeout=settings.connector_timeout_seconds, transport=transport
        )

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _credentials(self) -> tuple[str, str]:
        if not (self._client_id and self._client_secret):
            raise self._unsupported("oauth")
        return self._client_id, self._client_secret

    # --- OAuth (Google, Webanwendung, mit PKCE) --------------------------------------------

    def authorization_url(
        self, *, state: str, code_challenge: str, redirect_uri: str, write: bool = False
    ) -> str:
        client_id, _ = self._credentials()
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": WRITE_SCOPE if write else SCOPE,
            "access_type": "offline",  # Refresh-Token für Hintergrundjobs
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        client_id, secret = self._credentials()
        payload = self._oauth.post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return tokens_from_payload(self.platform, payload)

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        client_id, secret = self._credentials()
        payload = self._oauth.post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": secret,
            },
        )
        return tokens_from_payload(self.platform, payload, previous_refresh=refresh_token)

    def revoke(self, tokens: OAuthTokens) -> bool:
        token = tokens.refresh_token or tokens.access_token
        return self._oauth.post_status(REVOKE_URL, {"token": token}) == 200

    # --- Lesen ---------------------------------------------------------------------------

    def get_account(self, access_token: str) -> RemoteAccount:
        data = self._api.get("channels", access_token, {"part": "snippet", "mine": "true"})
        items = data.get("items") or []
        if not items:
            raise ConnectorAccessError(
                self.platform,
                "Das Google-Konto hat keinen YouTube-Kanal – ohne Kanal gibt es keine Playlists.",
                status=404,
            )
        channel = items[0]
        return RemoteAccount(
            external_id=str(channel.get("id") or ""),
            display_name=str((channel.get("snippet") or {}).get("title") or "YouTube-Kanal"),
        )

    def list_playlists(self, access_token: str, account: RemoteAccount) -> list[RemotePlaylistInfo]:
        result: list[RemotePlaylistInfo] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails",
                "mine": "true",
                "maxResults": PAGE_SIZE,
            }
            if token:
                params["pageToken"] = token
            page = self._api.get("playlists", access_token, params)
            for p in page.get("items") or []:
                snippet = p.get("snippet") or {}
                count = (p.get("contentDetails") or {}).get("itemCount")
                result.append(
                    RemotePlaylistInfo(
                        platform=self.platform,
                        external_id=str(p["id"]),
                        name=str(snippet.get("title") or "Ohne Namen"),
                        track_count=count,
                        url=f"https://www.youtube.com/playlist?list={p['id']}",
                        owner_name=snippet.get("channelTitle") or account.display_name,
                        description=snippet.get("description") or None,
                        owned=True,
                        items_accessible=True,
                        version=f"{p.get('etag', '')}:{count}",
                    )
                )
            token = page.get("nextPageToken")
            if not token:
                return result

    def iter_playlist_tracks(self, access_token: str, playlist_id: str) -> Iterator[TrackPage]:
        token: str | None = None
        while True:
            params: dict[str, Any] = {
                "part": "snippet,contentDetails,status",
                "playlistId": playlist_id,
                "maxResults": PAGE_SIZE,
            }
            if token:
                params["pageToken"] = token
            page = self._api.get("playlistItems", access_token, params)
            entries = [e for e in page.get("items") or [] if isinstance(e, dict)]
            video_ids = [
                vid for e in entries if (vid := (e.get("contentDetails") or {}).get("videoId"))
            ]
            videos = self._videos(access_token, video_ids)
            tracks = [self._map_entry(e, videos) for e in entries]
            yield TrackPage(tracks=tracks, total=(page.get("pageInfo") or {}).get("totalResults"))
            token = page.get("nextPageToken")
            if not token:
                return

    def get_tracks(
        self, access_token: str, external_ids: Sequence[str]
    ) -> dict[str, RemoteTrack | None]:
        result: dict[str, RemoteTrack | None] = {}
        ids = list(dict.fromkeys(external_ids))
        for start in range(0, len(ids), PAGE_SIZE):
            chunk = ids[start : start + PAGE_SIZE]
            videos = self._videos(access_token, chunk)
            for vid in chunk:
                video = videos.get(vid)
                result[vid] = self._map_video(vid, video) if video else None
        return result

    def search(self, access_token: str, query: TrackQuery, limit: int = 5) -> list[RemoteTrack]:
        # search.list hat ein eigenes Tageslimit (100 Aufrufe) – sparsam verwenden
        data = self._api.get(
            "search",
            access_token,
            {
                "part": "snippet",
                "type": "video",
                "videoCategoryId": "10",  # Musik
                "q": f"{query.artist} {query.title}",
                "maxResults": max(1, min(limit, 10)),
            },
        )
        ids = [
            str((i.get("id") or {}).get("videoId"))
            for i in data.get("items") or []
            if (i.get("id") or {}).get("videoId")
        ]
        videos = self._videos(access_token, ids)
        return [self._map_video(vid, videos[vid]) for vid in ids if vid in videos]

    # --- Schreiben (nur Synchronisierung, je Aufruf 50 Einheiten) ------------------------

    def create_remote_playlist(
        self, access_token: str, name: str, description: str | None
    ) -> RemotePlaylistInfo:
        data = self._api.write(
            "POST",
            "playlists",
            access_token,
            params={"part": "snippet,status"},
            json={
                "snippet": {"title": name[:150], "description": (description or "")[:5000]},
                "status": {"privacyStatus": "private"},
            },
        )
        playlist_id = str(data["id"])
        return RemotePlaylistInfo(
            platform=self.platform,
            external_id=playlist_id,
            name=str((data.get("snippet") or {}).get("title") or name),
            track_count=0,
            url=f"https://www.youtube.com/playlist?list={playlist_id}",
            owned=True,
            items_accessible=True,
            version=data.get("etag"),
        )

    def _item_body(self, playlist_id: str, video_id: str) -> dict[str, Any]:
        return {
            "playlistId": playlist_id,
            "resourceId": {"kind": "youtube#video", "videoId": video_id},
        }

    def add_to_playlist(
        self, access_token: str, playlist_id: str, external_ids: Sequence[str]
    ) -> None:
        for video_id in external_ids:
            self._api.write(
                "POST",
                "playlistItems",
                access_token,
                params={"part": "snippet"},
                json={"snippet": self._item_body(playlist_id, video_id)},
            )

    def remove_from_playlist(
        self, access_token: str, playlist_id: str, item_ids: Sequence[str]
    ) -> None:
        for item_id in item_ids:
            self._api.write("DELETE", "playlistItems", access_token, params={"id": item_id})

    def move_in_playlist(
        self, access_token: str, playlist_id: str, item_id: str, external_id: str, position: int
    ) -> None:
        self._api.write(
            "PUT",
            "playlistItems",
            access_token,
            params={"part": "snippet"},
            json={
                "id": item_id,
                "snippet": {**self._item_body(playlist_id, external_id), "position": position},
            },
        )

    # --- Abbildung -------------------------------------------------------------------------

    def _videos(self, access_token: str, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        data = self._api.get(
            "videos",
            access_token,
            {"part": "snippet,contentDetails,status", "id": ",".join(ids), "maxResults": PAGE_SIZE},
        )
        return {str(v["id"]): v for v in data.get("items") or [] if v.get("id")}

    def _map_video(self, vid: str, video: dict[str, Any]) -> RemoteTrack:
        snippet = video.get("snippet") or {}
        status = video.get("status") or {}
        artist = channel_artist(snippet.get("channelTitle"))
        # Private Videos liefert die API nur dem Besitzer – für dieses Konto also abspielbar
        private = status.get("privacyStatus") == "private"
        return RemoteTrack(
            platform=self.platform,
            external_id=vid,
            title=str(snippet.get("title") or ""),
            artists=(artist,) if artist else (),
            duration_ms=parse_iso_duration((video.get("contentDetails") or {}).get("duration")),
            url=f"https://www.youtube.com/watch?v={vid}",
            note="Privates Video (nur für das verbundene Konto sichtbar)" if private else None,
        )

    def _map_entry(self, entry: dict[str, Any], videos: dict[str, dict[str, Any]]) -> RemoteTrack:
        snippet = entry.get("snippet") or {}
        vid = str((entry.get("contentDetails") or {}).get("videoId") or "")
        title = str(snippet.get("title") or "")
        video = videos.get(vid)
        # Nicht von videos.list geliefert = gelöscht oder privat (fremdes Video)
        if not vid or video is None or title in _UNAVAILABLE_TITLES:
            return RemoteTrack(
                self.platform,
                vid,
                "" if title in _UNAVAILABLE_TITLES else title,
                (),
                available=False,
                note="Video gelöscht oder privat",
                item_id=entry.get("id"),
            )
        track = self._map_video(vid, video)
        artist = channel_artist(snippet.get("videoOwnerChannelTitle")) or (
            track.artists[0] if track.artists else None
        )
        return RemoteTrack(
            platform=self.platform,
            external_id=vid,
            title=track.title or title,
            artists=(artist,) if artist else (),
            duration_ms=track.duration_ms,
            url=track.url,
            available=track.available,
            note=track.note,
            item_id=entry.get("id"),
        )
