"""SoundCloud API – vorbereitet, lesend.

Voraussetzung laut SoundCloud (developers.soundcloud.com/docs/api/register-app): Eine eigene
API-App und damit Client-ID/Secret gibt es nur mit einem **Artist-Pro-Abo**. Ohne eigene
Zugangsdaten bleibt der Connector deaktiviert – es gibt keinen Umgehungsmechanismus (keine
fremden Client-IDs, kein Scraping). SoundCloud-Links lassen sich weiterhin von Hand oder per
CSV-Import pflegen.

Mit Zugangsdaten: OAuth 2.1 mit PKCE (Pflicht), Header ``Authorization: OAuth <token>``,
Refresh-Tokens sind einmalig verwendbar, ca. 100 Anfragen pro Minute.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from musichub.connectors.base import (
    Capability,
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
from musichub.domain.platform_urls import PlatformUrlError, parse_url
from musichub.domain.types import Platform

AUTHORIZE_URL = "https://secure.soundcloud.com/authorize"
TOKEN_URL = "https://secure.soundcloud.com/oauth/token"
API_URL = "https://api.soundcloud.com"
PAGE_SIZE = 50


class SoundCloudConnector(PlatformConnector):
    platform = Platform.SOUNDCLOUD
    capabilities = frozenset({Capability.OAUTH, Capability.READ_PLAYLISTS})
    #: SoundCloud kennt keine feingranularen Scopes
    scopes = ()

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self._client_id = settings.soundcloud_client_id
        secret = settings.soundcloud_client_secret
        self._client_secret = secret.get_secret_value() if secret else None
        self._api = ApiClient(
            self.platform,
            API_URL,
            timeout=settings.connector_timeout_seconds,
            transport=transport,
            auth_scheme="OAuth",
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

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        client_id, _ = self._credentials()
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{AUTHORIZE_URL}?{urlencode(query)}"

    def exchange_code(self, *, code: str, code_verifier: str, redirect_uri: str) -> OAuthTokens:
        client_id, secret = self._credentials()
        payload = self._oauth.post_form(
            TOKEN_URL,
            {
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
                "code": code,
            },
        )
        return tokens_from_payload(self.platform, payload)

    def refresh_tokens(self, refresh_token: str) -> OAuthTokens:
        client_id, secret = self._credentials()
        payload = self._oauth.post_form(
            TOKEN_URL,
            {
                "grant_type": "refresh_token",
                "client_id": client_id,
                "client_secret": secret,
                "refresh_token": refresh_token,
            },
        )
        # Refresh-Tokens sind einmalig: das neue Token muss sofort gespeichert werden
        return tokens_from_payload(self.platform, payload)

    def get_account(self, access_token: str) -> RemoteAccount:
        me = self._api.get("me", access_token)
        return RemoteAccount(
            external_id=str(me.get("urn") or me.get("id") or ""),
            display_name=str(me.get("username") or "SoundCloud-Konto"),
        )

    def list_playlists(self, access_token: str, account: RemoteAccount) -> list[RemotePlaylistInfo]:
        result: list[RemotePlaylistInfo] = []
        url: str | None = "me/playlists"
        params: dict[str, Any] | None = {
            "show_tracks": "false",
            "linked_partitioning": "true",
            "limit": PAGE_SIZE,
        }
        while url:
            page = self._api.get(url, access_token, params)
            params = None
            for p in page.get("collection") or []:
                urn = p.get("urn")
                if not urn:
                    continue
                user = p.get("user") or {}
                owned = not user.get("urn") or user.get("urn") == account.external_id
                result.append(
                    RemotePlaylistInfo(
                        platform=self.platform,
                        external_id=str(urn),
                        name=str(p.get("title") or "Ohne Namen"),
                        track_count=p.get("track_count"),
                        url=p.get("permalink_url"),
                        owner_name=user.get("username") or account.display_name,
                        description=p.get("description") or None,
                        owned=owned,
                        items_accessible=True,
                        version=str(p.get("last_modified") or "") or None,
                    )
                )
            url = page.get("next_href")
        return result

    def iter_playlist_tracks(self, access_token: str, playlist_id: str) -> Iterator[TrackPage]:
        url: str | None = f"playlists/{quote(playlist_id, safe='')}/tracks"
        params: dict[str, Any] | None = {"linked_partitioning": "true", "limit": PAGE_SIZE}
        while url:
            page = self._api.get(url, access_token, params)
            params = None
            tracks = [self._map(t) for t in page.get("collection") or [] if isinstance(t, dict)]
            yield TrackPage(tracks=tracks)
            url = page.get("next_href")

    def _map(self, t: dict[str, Any]) -> RemoteTrack:
        url = t.get("permalink_url")
        try:
            external_id = parse_url(str(url)).external_id if url else ""
        except PlatformUrlError:
            external_id = ""
        user = t.get("user") or {}
        artist = t.get("metadata_artist") or user.get("username")
        available = t.get("access") != "blocked" and bool(external_id)
        return RemoteTrack(
            platform=self.platform,
            external_id=external_id,
            title=str(t.get("title") or ""),
            artists=(str(artist),) if artist else (),
            duration_ms=t.get("duration") or None,
            isrc=t.get("isrc") or None,
            url=url,
            available=available,
            note=None if available else "Track gesperrt oder ohne gültigen Link",
        )
