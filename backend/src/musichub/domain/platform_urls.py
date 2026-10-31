"""Plattform-Links erkennen und erzeugen – ohne Netzwerkzugriff.

Damit lassen sich Quellen manuell per eingefügtem Link anlegen, bevor irgendein
Connector konfiguriert ist:

* Spotify:    https://open.spotify.com/track/<id>, spotify:track:<id>
              (auch mit Länderpfad wie /intl-de/track/<id>)
* YouTube:    youtube.com/watch?v=<id>, youtu.be/<id>, music.youtube.com/watch?v=<id>,
              youtube.com/shorts/<id>
* SoundCloud: soundcloud.com/<nutzer>/<track>  -> externe ID ist der Permalink
              "<nutzer>/<track>". Die numerische ID liefert erst der Connector.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from musichub.domain.types import Platform

_SPOTIFY_ID = re.compile(r"^[A-Za-z0-9]{22}$")
_YOUTUBE_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_SOUNDCLOUD_PART = re.compile(r"^[A-Za-z0-9_-]+$")
_SOUNDCLOUD_RESERVED = {"discover", "search", "stream", "you", "upload", "charts", "pages", "jobs"}


class PlatformUrlError(ValueError):
    """Link oder ID passt zu keinem unterstützten Format."""


@dataclass(frozen=True, slots=True)
class ParsedSource:
    platform: Platform
    external_id: str
    url: str


def canonical_url(platform: Platform, external_id: str) -> str:
    match platform:
        case Platform.SPOTIFY:
            return f"https://open.spotify.com/track/{external_id}"
        case Platform.YOUTUBE:
            return f"https://www.youtube.com/watch?v={external_id}"
        case Platform.SOUNDCLOUD:
            return f"https://soundcloud.com/{external_id}"


def validate_external_id(platform: Platform, external_id: str) -> str:
    value = external_id.strip()
    match platform:
        case Platform.SPOTIFY:
            if value.startswith("spotify:track:"):
                value = value.removeprefix("spotify:track:")
            ok = bool(_SPOTIFY_ID.match(value))
        case Platform.YOUTUBE:
            ok = bool(_YOUTUBE_ID.match(value))
        case Platform.SOUNDCLOUD:
            value = value.strip("/").lower()
            parts = value.split("/")
            ok = (
                len(parts) == 2
                and all(_SOUNDCLOUD_PART.match(p) for p in parts)
                and parts[0] not in _SOUNDCLOUD_RESERVED
            )
    if not ok:
        raise PlatformUrlError(f"Ungültige {platform.value}-ID: „{external_id}“")
    return value


def parse_url(raw: str) -> ParsedSource:
    value = raw.strip()
    if value.startswith("spotify:track:"):
        external_id = validate_external_id(Platform.SPOTIFY, value)
        return ParsedSource(
            Platform.SPOTIFY, external_id, canonical_url(Platform.SPOTIFY, external_id)
        )

    if "://" not in value:
        value = f"https://{value}"
    parts = urlsplit(value)
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    segments = [s for s in parts.path.split("/") if s]

    platform: Platform | None = None
    candidate: str | None = None

    if host == "open.spotify.com":
        platform = Platform.SPOTIFY
        if "track" in segments:
            i = segments.index("track")
            candidate = segments[i + 1] if i + 1 < len(segments) else None
    elif host in {"youtube.com", "music.youtube.com"}:
        platform = Platform.YOUTUBE
        if segments[:1] == ["watch"]:
            candidate = (parse_qs(parts.query).get("v") or [None])[0]
        elif len(segments) >= 2 and segments[0] in {"shorts", "embed", "live"}:
            candidate = segments[1]
    elif host == "youtu.be":
        platform = Platform.YOUTUBE
        candidate = segments[0] if segments else None
    elif host == "soundcloud.com":
        platform = Platform.SOUNDCLOUD
        if len(segments) >= 2 and segments[1] not in {"sets", "tracks", "likes", "reposts"}:
            candidate = f"{segments[0]}/{segments[1]}"

    if platform is None:
        raise PlatformUrlError(f"Link gehört zu keiner unterstützten Plattform: „{raw.strip()}“")
    if not candidate:
        raise PlatformUrlError(f"Link enthält keinen einzelnen Track: „{raw.strip()}“")
    external_id = validate_external_id(platform, candidate)
    return ParsedSource(platform, external_id, canonical_url(platform, external_id))
