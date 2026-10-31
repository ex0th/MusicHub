"""Abgleich importierter Datensätze mit der Bibliothek – reine Funktionen ohne I/O.

Reihenfolge der Identitätsmerkmale (das erste Treffermerkmal gewinnt):
1. ``musichub_id`` aus einem MusicHub-Export
2. bestätigte Plattform-Quelle (Plattform + ID)
3. ISRC
4. Identitätsschlüssel aus normalisiertem Titel, Artists und Version

Treffer über 1–4 gelten als „bereits vorhanden“. Ohne exakten Treffer prüft die
Matching-Engine ähnliche Tracks desselben Artists; ab ``POTENTIAL_THRESHOLD`` wird ein
„mögliches Duplikat“ gemeldet, über das der Nutzer entscheidet.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from musichub.domain.types import MatchLevel
from musichub.matching.engine import (
    TrackFacts,
    compare,
    identity_key,
    normalize_artists,
    version_of,
)
from musichub.transfer.records import ImportRecord

#: Ab „Prüfung“ (Confidence ≥ 60) wird ein mögliches Duplikat gemeldet,
#: ab „wahrscheinlich“ (≥ 85) schlägt der Assistent „vorhandenen Track verwenden“ vor.
POTENTIAL_THRESHOLD = 0.60
SUGGEST_EXISTING_THRESHOLD = 0.85


def normalized_version(title: str, version: str | None) -> str:
    """Versionsschlüssel aus Versionsfeld bzw. Titel; leer = Original."""
    return version_of(title, version).key


@dataclass(slots=True)
class LibraryTrack:
    id: str
    title: str
    artist: str
    version: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    #: bestätigte Quellen als "plattform:id"
    sources: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class Match:
    track_id: str
    reason: str  # musichub_id | source | isrc | metadata | similar
    score: float


def record_source_keys(record: ImportRecord) -> list[str]:
    return [f"{s.platform}:{s.external_id}" for s in record.sources]


class LibraryIndex:
    """Nachschlagetabellen über alle Tracks der Bibliothek."""

    def __init__(self, tracks: list[LibraryTrack] | None = None) -> None:
        self.by_id: dict[str, LibraryTrack] = {}
        self.by_source: dict[str, str] = {}
        self.by_isrc: dict[str, str] = {}
        self.by_key: dict[str, str] = {}
        self.by_artist: dict[str, list[LibraryTrack]] = defaultdict(list)
        for track in tracks or []:
            self.add(track)

    def add(self, track: LibraryTrack) -> None:
        self.by_id[track.id] = track
        for key in track.sources:
            self.by_source.setdefault(key, track.id)
        if track.isrc:
            self.by_isrc.setdefault(track.isrc, track.id)
        self.by_key.setdefault(identity_key(track.title, track.artist, track.version), track.id)
        for name in normalize_artists(track.artist):
            self.by_artist[name].append(track)

    def exact(self, record: ImportRecord) -> Match | None:
        if record.musichub_id and record.musichub_id in self.by_id:
            return Match(record.musichub_id, "musichub_id", 1.0)
        for key in record_source_keys(record):
            if key in self.by_source:
                return Match(self.by_source[key], "source", 1.0)
        if record.isrc and record.isrc in self.by_isrc:
            return Match(self.by_isrc[record.isrc], "isrc", 1.0)
        if record.title and record.artist:
            key = identity_key(record.title, record.artist, record.version)
            if key in self.by_key:
                return Match(self.by_key[key], "metadata", 1.0)
        return None

    def similar(self, record: ImportRecord) -> Match | None:
        if not (record.title and record.artist):
            return None
        candidates: dict[str, LibraryTrack] = {}
        for name in normalize_artists(record.artist):
            for track in self.by_artist.get(name, []):
                candidates[track.id] = track
        if not candidates:
            return None
        local = TrackFacts(
            title=record.title,
            artist=record.artist,
            version=record.version,
            duration_ms=record.duration_ms,
            isrc=record.isrc,
        )
        best: Match | None = None
        for track in candidates.values():
            other = TrackFacts(
                title=track.title,
                artist=track.artist,
                version=track.version,
                duration_ms=track.duration_ms,
                isrc=track.isrc,
            )
            result = compare(local, other)
            if result.level is MatchLevel.DIFFERENT:
                continue
            value = result.confidence / 100
            if best is None or value > best.score:
                best = Match(track.id, "similar", value)
        return best


def file_identity_keys(record: ImportRecord) -> list[str]:
    """Schlüssel, über die gleiche Tracks innerhalb einer Datei erkannt werden."""
    keys: list[str] = []
    if record.musichub_id:
        keys.append(f"id:{record.musichub_id}")
    keys += [f"src:{k}" for k in record_source_keys(record)]
    if record.isrc:
        keys.append(f"isrc:{record.isrc}")
    if record.title and record.artist:
        keys.append(f"key:{identity_key(record.title, record.artist, record.version)}")
    return keys
