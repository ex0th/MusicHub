"""Matching-Engine: bewertet, wie gut ein Plattform-Track zu einem lokalen Track passt.

Reine Funktionen ohne I/O – leicht testbar und von Connectoren/DB unabhängig.

Bewertung:
1. Gleiche ISRC  -> Score 1.0, Methode ``isrc``.
2. Sonst gewichteter Fuzzy-Score aus Titel (0.5), Interpret (0.35) und Dauer (0.15).
   Versions-Marker (Remix, Live, Acoustic …) müssen übereinstimmen, sonst starker Abzug,
   damit ein Remix nie als Original verknüpft wird.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from musichub.domain.types import MatchMethod

#: Ab diesem Score wird automatisch verknüpft, darunter nur als Kandidat vorgeschlagen.
AUTO_LINK_THRESHOLD = 0.92
CANDIDATE_THRESHOLD = 0.6

_VERSION_MARKERS = (
    "remix",
    "live",
    "acoustic",
    "instrumental",
    "edit",
    "extended",
    "remaster",
    "demo",
    "karaoke",
    "cover",
    "sped up",
    "slowed",
    "nightcore",
)
_NOISE = re.compile(
    r"\b(official\s+(music\s+)?video|official\s+audio|lyric(s)?\s+video|audio|hd|hq|4k|mv)\b"
)
_FEAT = re.compile(r"\b(feat\.?|ft\.?|featuring|with)\b.*$")
_BRACKETS = re.compile(r"[\(\[\{].*?[\)\]\}]")
_NON_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")
_ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\bx\b|\band\b|\bund\b|\bfeat\.?|\bft\.?|\bvs\.?)\s*")


@dataclass(frozen=True, slots=True)
class TrackFingerprint:
    title: str
    artist: str
    duration_ms: int | None = None
    isrc: str | None = None


@dataclass(frozen=True, slots=True)
class MatchResult:
    score: float
    method: MatchMethod
    title_score: float
    artist_score: float
    duration_score: float | None

    @property
    def auto_link(self) -> bool:
        return self.score >= AUTO_LINK_THRESHOLD

    @property
    def is_candidate(self) -> bool:
        return self.score >= CANDIDATE_THRESHOLD


def _fold(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in value if not unicodedata.combining(c))


def version_markers(title: str) -> frozenset[str]:
    folded = _fold(title)
    return frozenset(m for m in _VERSION_MARKERS if re.search(rf"\b{m}\b", folded))


def normalize_title(title: str) -> str:
    value = _fold(title)
    value = _NOISE.sub(" ", value)
    value = _BRACKETS.sub(" ", value)
    value = _FEAT.sub(" ", value)
    # "Artist - Title" (typisch für YouTube): nur den Titelteil behalten
    if " - " in value:
        head, tail = value.split(" - ", 1)
        # "Titel - Extended Mix" (Spotify) behält den Titel, "Artist - Titel" (YouTube) den Rest
        value = head if version_markers(tail) or "mix" in tail or "version" in tail else tail
    value = _NON_WORD.sub(" ", value)
    return _SPACES.sub(" ", value).strip()


def normalize_artists(artist: str) -> frozenset[str]:
    value = _fold(artist)
    value = re.sub(r"\s*-\s*topic$", "", value)  # YouTube-"Topic"-Kanäle
    value = re.sub(r"vevo$", "", value)
    parts = (_SPACES.sub(" ", _NON_WORD.sub(" ", p)).strip() for p in _ARTIST_SPLIT.split(value))
    return frozenset(p for p in parts if p)


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _artist_score(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    if a & b:
        # Überlappung der Interpreten (Jaccard), mindestens 0.8 bei gemeinsamem Hauptinterpreten
        return max(0.8, len(a & b) / len(a | b))
    # Kein exakter Treffer: beste Fuzzy-Übereinstimmung zweier Namen
    return max(_ratio(x, y) for x in a for y in b)


def _duration_score(a: int | None, b: int | None) -> float | None:
    if not a or not b:
        return None
    diff = abs(a - b) / 1000
    if diff <= 2:
        return 1.0
    if diff >= 30:
        return 0.0
    return 1.0 - (diff - 2) / 28


def score(local: TrackFingerprint, remote: TrackFingerprint) -> MatchResult:
    if local.isrc and remote.isrc and local.isrc.upper() == remote.isrc.upper():
        return MatchResult(
            1.0, MatchMethod.ISRC, 1.0, 1.0, _duration_score(local.duration_ms, remote.duration_ms)
        )

    title_s = _ratio(normalize_title(local.title), normalize_title(remote.title))
    artist_s = _artist_score(normalize_artists(local.artist), normalize_artists(remote.artist))
    # YouTube-Titel enthalten oft den Interpreten ("Artist - Title"): als Fallback prüfen
    if artist_s < 0.8 and " - " in remote.title:
        artist_s = max(
            artist_s,
            _artist_score(
                normalize_artists(local.artist), normalize_artists(remote.title.split(" - ", 1)[0])
            ),
        )
    duration_s = _duration_score(local.duration_ms, remote.duration_ms)

    if duration_s is None:
        total = (title_s * 0.5 + artist_s * 0.35) / 0.85
    else:
        total = title_s * 0.5 + artist_s * 0.35 + duration_s * 0.15

    if version_markers(local.title) != version_markers(remote.title):
        total *= 0.6

    return MatchResult(
        round(total, 4), MatchMethod.FUZZY, round(title_s, 4), round(artist_s, 4), duration_s
    )


def rank(
    local: TrackFingerprint, candidates: list[TrackFingerprint]
) -> list[tuple[int, MatchResult]]:
    """Index und Ergebnis aller Kandidaten über ``CANDIDATE_THRESHOLD``, bester zuerst."""
    scored = [(i, score(local, c)) for i, c in enumerate(candidates)]
    return sorted((s for s in scored if s[1].is_candidate), key=lambda s: s[1].score, reverse=True)
