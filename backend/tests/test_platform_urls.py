import pytest

from musichub.domain.platform_urls import (
    PlatformUrlError,
    canonical_url,
    parse_url,
    validate_external_id,
)
from musichub.domain.types import Platform

SPOTIFY_ID = "4cOdK2wGLETKBW3PvgPWqT"


@pytest.mark.parametrize(
    ("url", "platform", "external_id"),
    [
        (f"https://open.spotify.com/track/{SPOTIFY_ID}?si=abc", Platform.SPOTIFY, SPOTIFY_ID),
        (f"https://open.spotify.com/intl-de/track/{SPOTIFY_ID}", Platform.SPOTIFY, SPOTIFY_ID),
        (f"spotify:track:{SPOTIFY_ID}", Platform.SPOTIFY, SPOTIFY_ID),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42", Platform.YOUTUBE, "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", Platform.YOUTUBE, "dQw4w9WgXcQ"),
        ("music.youtube.com/watch?v=dQw4w9WgXcQ", Platform.YOUTUBE, "dQw4w9WgXcQ"),
        ("https://m.youtube.com/shorts/dQw4w9WgXcQ", Platform.YOUTUBE, "dQw4w9WgXcQ"),
        (
            "https://soundcloud.com/Artist-Name/my_track?in=x",
            Platform.SOUNDCLOUD,
            "artist-name/my_track",
        ),
    ],
)
def test_parse_supported_urls(url: str, platform: Platform, external_id: str) -> None:
    parsed = parse_url(url)
    assert parsed.platform is platform
    assert parsed.external_id == external_id
    assert parsed.url == canonical_url(platform, external_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/track/1",
        f"https://open.spotify.com/album/{SPOTIFY_ID}",
        "https://www.youtube.com/channel/UC123",
        "https://soundcloud.com/artist/sets/playlist",
        "https://soundcloud.com/discover/foo",
        "https://youtu.be/too-short",
    ],
)
def test_reject_unsupported_urls(url: str) -> None:
    with pytest.raises(PlatformUrlError):
        parse_url(url)


def test_validate_external_ids() -> None:
    assert validate_external_id(Platform.SPOTIFY, f"spotify:track:{SPOTIFY_ID}") == SPOTIFY_ID
    assert validate_external_id(Platform.SOUNDCLOUD, "/A/B/") == "a/b"
    with pytest.raises(PlatformUrlError):
        validate_external_id(Platform.SPOTIFY, "nope")
    with pytest.raises(PlatformUrlError):
        validate_external_id(Platform.SOUNDCLOUD, "only-one-part")
