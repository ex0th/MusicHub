"""Spotify Web API – lesend (Playlists des Kontos, Playlist-Inhalte, Track-Metadaten).

Stand der API (Februar-2026-Änderungen, siehe docs/CONNECTORS.md):
* Playlist-Inhalte: ``GET /playlists/{id}/items``; Feld ``item`` (``track`` veraltet).
* Development Mode: App-Besitzer braucht Premium, höchstens 5 Nutzer, Inhalte nur für
  eigene oder kollaborative Playlists (sonst 403), keine Sammelabfrage ``GET /tracks``.
* Scopes: ``playlist-read-private`` und ``playlist-read-collaborative`` – nichts Schreibendes.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
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
)
from musichub.connectors.http import ApiClient, OAuthClient
from musichub.connectors.oauth import tokens_from_payload
from musichub.core.config import Settings
from musichub.domain.types import Platform

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_URL = "https://api.spotify.com/v1"
PAGE_SIZE = 50


class SpotifyConnector(PlatformConnector):
    platform = Platform.SPOTIFY
    capabilities = frozenset(
        {Capability.OAUTH, Capability.READ_PLAYLISTS, Capability.REFRESH_METADATA}
    )
    scopes = ("playlist-read-private", "playlist-read-collaborative")

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client_id = settings.spotify_client_id
        secret = settings.spotify_client_secret
        self._client_secret = secret.get_secret_value() if secret else None
        self._api = ApiClient(
            self.platform, API_URL, timeout=settings.connector_timeout_seconds, transport=transport
        )
        self._oauth = OAuthClient(
            self.platform, timeout=settings.connector_timeout_seconds, transport=transport
        )

    def is_configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _basic(self) -> tuple[str, str]:
        if not (self._client_id and self._client_secret):
            raise self._unsupported("oauth")
        return self._client_id, self._client_secret

    # --- OAuth (Authorization Code Flow mit Client-Secret, serverseitig) -----------------

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        self._basic()
        query = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(self.scopes),
            "state": state,
        }
        # Dokumentierter Authorization Code Flow für Server mit Client-Secret: ohne PKCE.
        # CSRF-Schutz über ``state`` (einmalig, an den Nutzer gebunden, 10 min gültig).
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        payload = self._oauth.post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
            basic_auth=self._basic(),
        )
        return tokens_from_payload(self.platform, payload)

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        payload = self._oauth.post_form(
            TOKEN_URL,
            {"grant_type": "refresh_token", "refresh_token": refresh_token},
            basic_auth=self._basic(),
        )
        return tokens_from_payload(self.platform, payload, previous_refresh=refresh_token)

    # Spotify bietet keinen Widerrufs-Endpunkt: Zugriff unter spotify.com/account/apps entfernen.

    # --- Lesen ---------------------------------------------------------------------------

    def get_account(self, access_token: str) -> RemoteAccount:
        me = self._api.get("me", access_token)
        return RemoteAccount(
            external_id=str(me.get("id") or ""),
            display_name=str(me.get("display_name") or me.get("id") or "Spotify-Konto"),
        )

    def list_playlists(self, access_token: str, account: RemoteAccount) -> list[RemotePlaylistInfo]:
        result: list[RemotePlaylistInfo] = []
        url: str | None = "me/playlists"
        params: dict[str, Any] | None = {"limit": PAGE_SIZE}
        while url:
            page = self._api.get(url, access_token, params)
            params = None  # „next“ enthält bereits alle Parameter
            for p in page.get("items") or []:
                if not isinstance(p, dict) or not p.get("id"):
                    continue
                owner = p.get("owner") or {}
                owned = owner.get("id") == account.external_id
                counts = p.get("items") or p.get("tracks") or {}
                result.append(
                    RemotePlaylistInfo(
                        platform=self.platform,
                        external_id=p["id"],
                        name=str(p.get("name") or "Ohne Namen"),
                        track_count=counts.get("total") if isinstance(counts, dict) else None,
                        url=(p.get("external_urls") or {}).get("spotify"),
                        owner_name=owner.get("display_name") or owner.get("id"),
                        description=p.get("description") or None,
                        owned=owned,
                        items_accessible=owned or bool(p.get("collaborative")),
                        version=p.get("snapshot_id"),
                    )
                )
            url = page.get("next")
        return result

    def iter_playlist_tracks(self, access_token: str, playlist_id: str) -> Iterator[TrackPage]:
        url: str | None = f"playlists/{playlist_id}/items"
        params: dict[str, Any] | None = {"limit": PAGE_SIZE, "additional_types": "track"}
        while url:
            try:
                page = self._api.get(url, access_token, params)
            except ConnectorAccessError as exc:
                if exc.status == 403:
                    raise ConnectorAccessError(
                        self.platform,
                        "Spotify gibt die Inhalte dieser Playlist nicht frei. Im Development "
                        "Mode sind nur eigene oder kollaborative Playlists lesbar.",
                        status=403,
                    ) from None
                raise
            params = None
            tracks = [self._map_entry(e) for e in page.get("items") or [] if isinstance(e, dict)]
            yield TrackPage(tracks=tracks, total=page.get("total"))
            url = page.get("next")

    def get_tracks(
        self, access_token: str, external_ids: Sequence[str]
    ) -> dict[str, RemoteTrack | None]:
        # Sammelabfrage GET /tracks ist im Development Mode entfernt: einzeln abrufen
        result: dict[str, RemoteTrack | None] = {}
        for track_id in external_ids:
            try:
                data = self._api.get(f"tracks/{track_id}", access_token)
            except ConnectorAccessError as exc:
                if exc.status in (400, 404):
                    result[track_id] = None
                    continue
                raise
            result[track_id] = self._map_track(data)
        return result

    # --- Abbildung -------------------------------------------------------------------------

    def _map_entry(self, entry: dict[str, Any]) -> RemoteTrack:
        item = entry.get("item") or entry.get("track")
        if not isinstance(item, dict):
            return RemoteTrack(
                self.platform, "", "", (), available=False, note="Track nicht mehr verfügbar"
            )
        if item.get("type") == "episode":
            return RemoteTrack(
                self.platform,
                "",
                str(item.get("name") or ""),
                (),
                available=False,
                note="Podcast-Episode übersprungen",
            )
        if entry.get("is_local") or item.get("is_local"):
            return RemoteTrack(
                self.platform,
                "",
                str(item.get("name") or ""),
                tuple(a.get("name", "") for a in item.get("artists") or [] if a.get("name")),
                duration_ms=item.get("duration_ms") or None,
                album=(item.get("album") or {}).get("name") or None,
                note="Lokale Datei (ohne Spotify-ID)",
            )
        return self._map_track(item)

    def _map_track(self, item: dict[str, Any]) -> RemoteTrack:
        album = item.get("album") or {}
        release = album.get("release_date")
        precision = album.get("release_date_precision")
        restrictions = item.get("restrictions") or {}
        available = item.get("is_playable") is not False and not restrictions
        return RemoteTrack(
            platform=self.platform,
            external_id=str(item.get("id") or ""),
            title=str(item.get("name") or ""),
            artists=tuple(a["name"] for a in item.get("artists") or [] if a.get("name")),
            duration_ms=item.get("duration_ms") or None,
            isrc=(item.get("external_ids") or {}).get("isrc"),
            url=(item.get("external_urls") or {}).get("spotify"),
            album=album.get("name") or None,
            release_date=release if precision == "day" and isinstance(release, str) else None,
            available=available,
            note=None if available else "Auf Spotify derzeit nicht abspielbar",
        )
