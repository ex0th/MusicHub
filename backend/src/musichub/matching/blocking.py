"""Kandidatenpaare finden, ohne jeden Track mit jedem zu vergleichen (Blocking).

Zwei Tracks werden nur verglichen, wenn sie mindestens einen Schlüssel teilen:

- gleiche ISRC,
- gleicher normalisierter Basistitel (ohne Leerzeichen),
- gleicher Artist und gleicher Titelanfang (fängt Tippfehler und Zusätze im Titel ab).

Jede Lesart eines Tracks (auch „Artist - Titel“ aus YouTube) liefert eigene Schlüssel.
Sehr große Gruppen (z. B. hunderte Tracks namens „Intro“) werden gekappt, damit ein Scan
auch bei großen Sammlungen linear bleibt. Reine Funktion ohne I/O.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Hashable, Iterable
from itertools import combinations

from musichub.matching.engine import ParsedTrack

#: Gruppen über dieser Größe sind zu unspezifisch und werden übersprungen
MAX_BUCKET = 200
_PREFIX = 4


def blocking_keys(track: ParsedTrack) -> set[str]:
    keys: set[str] = set()
    if not track.matchable:
        return keys
    if track.facts.isrc:
        keys.add(f"isrc:{track.facts.isrc.upper()}")
    for reading in track.readings:
        title = reading.compact_title
        if not title:
            continue
        keys.add(f"title:{title}")
        for artist in reading.artists:
            keys.add(f"artist:{artist.replace(' ', '')}|{title[:_PREFIX]}")
    return keys


def candidate_pairs[K: Hashable](tracks: Iterable[tuple[K, ParsedTrack]]) -> set[tuple[K, K]]:
    """Paare (a, b) mit ``a < b``, die mindestens einen Blocking-Schlüssel teilen."""
    buckets: dict[str, list[K]] = defaultdict(list)
    for key, track in tracks:
        for block in blocking_keys(track):
            buckets[block].append(key)
    pairs: set[tuple[K, K]] = set()
    for members in buckets.values():
        if len(members) < 2 or len(members) > MAX_BUCKET:
            continue
        for a, b in combinations(sorted(set(members), key=str), 2):
            pairs.add((a, b))
    return pairs
