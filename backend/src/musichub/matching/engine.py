"""Matching-Engine: erkennt, ob zwei Track-Einträge dieselbe Aufnahme in derselben Version sind.

Reine Funktionen ohne I/O – unabhängig von Datenbank und Connectoren, vollständig testbar.

Ablauf von :func:`compare`:

1. **Zerlegen** (:func:`parse`): Titel und Artist werden normalisiert (Groß-/Kleinschreibung,
   Akzente, Satzzeichen, ``&``/``and``), Zusätze wie „Official Audio“, „[HD]“ oder
   „- Topic“ entfernt, Featuring-Angaben und Versionsangaben (Klammern, „ - Extended Mix“,
   Versionsfeld) getrennt erkannt. YouTube-Titel „Artist - Titel“ ergeben eine zweite Lesart.
2. **Grundwert** aus Titel- (max. 55) und Artist-Ähnlichkeit (max. 45).
3. **Zu- und Abschläge** für ISRC, Dauer, Album, Veröffentlichungsjahr, Featuring und
   Plattform-Metadaten.
4. **Obergrenzen** (Caps) für alles, was gegen dieselbe Version spricht – vor allem
   abweichende Versionen (Remix ≠ Original ≠ Extended Mix …). Ein Cap kann durch keinen
   Bonus aufgehoben werden.
5. **Stufe**: ``certain`` verlangt einen harten Beleg (gleiche ISRC oder exakter Titel,
   Artist und Version *und* Dauer ±2 s) und keine Blocker. Ohne Beleg endet ein Paar
   höchstens bei ``likely``.

Jeder Schritt hinterlässt einen :class:`Reason` – der Score ist damit nachvollziehbar.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from difflib import SequenceMatcher
from enum import StrEnum

from musichub.domain.types import MatchLevel

#: Wird in gespeicherten Kandidaten mitgeschrieben; bei Regeländerungen erhöhen.
ENGINE_VERSION = "2"

CERTAIN_MIN = 95
LIKELY_MIN = 85
REVIEW_MIN = 60

TITLE_WEIGHT = 55
ARTIST_WEIGHT = 45

PLATFORM_LABELS = {"spotify": "Spotify", "soundcloud": "SoundCloud", "youtube": "YouTube"}


# --- Eingabe ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackFacts:
    """Alles, was die Engine über einen Track weiß."""

    title: str
    artist: str
    version: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    album: str | None = None
    release_date: date | None = None
    #: bestätigte Plattform-Quellen: Plattform -> externe ID
    platforms: dict[str, str] = field(default_factory=dict)


# --- Normalisierung --------------------------------------------------------------------


def fold(value: str) -> str:
    """Kleinschreibung, ohne Akzente (``Rüfüs`` → ``rufus``)."""
    value = unicodedata.normalize("NFKD", value.casefold())
    return "".join(c for c in value if not unicodedata.combining(c))


_SPACES = re.compile(r"\s+")
_NON_WORD = re.compile(r"[^\w\s]")


def _clean(value: str) -> str:
    value = value.replace("&", " and ").replace("$", "s").replace("'", "").replace("’", "")
    return _SPACES.sub(" ", _NON_WORD.sub(" ", value)).replace("_", " ").strip()


def _compact(value: str) -> str:
    return value.replace(" ", "")


_DASH = re.compile(r"\s+[-–—]\s+")
_BRACKET = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]*)[\)\]\}]")
_FEAT_INLINE = re.compile(r"\s(?:feat\.?|ft\.?|featuring)\s+(.+)$")
_FEAT_BRACKET = re.compile(r"^(?:feat\.?|ft\.?|featuring|with)\s+(.+)$")
_PREFIX_NOISE = re.compile(r"^(?:premiere|exclusive|free download|free dl|new)\s*[:|]\s*")
_PIPE = re.compile(r"\s+[|/]{1,2}\s+")

_NOISE_WORDS = frozenset(
    {
        *("official", "officiel", "offizielles", "music", "video", "videoclip", "musikvideo"),
        *("audio", "lyric", "lyrics", "visualizer", "visualiser", "clip", "mv"),
        *("hd", "hq", "4k", "1080p", "720p", "free", "download", "dl", "out", "now"),
        *("premiere", "explicit", "clean", "uncensored", "release", "exclusive", "full"),
        *("song", "with", "and", "new"),
    }
)
_VIDEO_HINT = re.compile(r"\b(video|videoclip|visuali[sz]er|mv|musikvideo)\b")
_PROD = re.compile(r"^prod\.?\b|^produced by\b")

_ARTIST_SPLIT = re.compile(
    r"\s*(?:,|;|/|&|\+|\bx\b|\band\b|\bund\b|\bvs\.?|\bfeat\.?|\bft\.?|\bfeaturing\b|\bwith\b)\s*"
)
_ARTIST_CHANNEL_NOISE = re.compile(r"(?:\s*-\s*topic|vevo|\s+official)$")
_UNKNOWN_ARTISTS = frozenset(
    {"", "unknown", "unknown artist", "unbekannt", "various", "various artists", "va", "n a", "na"}
)
_UNKNOWN_TITLE = re.compile(r"^(?:unknown|untitled|unbekannt|track\s*\d*|audio\s*\d*|\d+)$")


def normalize_artist_name(name: str) -> str:
    value = _clean(fold(name))
    value = re.sub(r"^the ", "", value)
    return value


def split_artists(artist: str) -> tuple[frozenset[str], frozenset[str]]:
    """Artist-Feld → (Hauptartists, Featuring-Artists), jeweils normalisiert."""
    value = fold(artist).strip()
    value = _ARTIST_CHANNEL_NOISE.sub("", value)
    feat: set[str] = set()
    match = re.search(r"\s(?:feat\.?|ft\.?|featuring)\s+(.+)$", value)
    if match:
        feat = _split_names(match.group(1))
        value = value[: match.start()]
    return frozenset(_split_names(value)), frozenset(feat)


def _split_names(value: str) -> set[str]:
    return {n for n in (normalize_artist_name(p) for p in _ARTIST_SPLIT.split(value)) if n}


def normalize_artists(artist: str) -> frozenset[str]:
    """Alle Artists eines Artist-Felds (Haupt- und Featuring-Artists)."""
    main, feat = split_artists(artist)
    return main | feat


# --- Versionen -------------------------------------------------------------------------


class VersionKind(StrEnum):
    REMIX = "remix"
    VIP = "vip"
    BOOTLEG = "bootleg"
    EDIT = "edit"
    RADIO_EDIT = "radio_edit"
    EXTENDED = "extended"
    CLUB = "club"
    DUB = "dub"
    LIVE = "live"
    COVER = "cover"
    ACOUSTIC = "acoustic"
    INSTRUMENTAL = "instrumental"
    ACAPELLA = "acapella"
    REMASTER = "remaster"
    RERECORDED = "rerecorded"
    DEMO = "demo"
    MASHUP = "mashup"
    SPED_UP = "sped_up"
    SLOWED = "slowed"
    NIGHTCORE = "nightcore"
    KARAOKE = "karaoke"


VERSION_LABELS: dict[VersionKind, str] = {
    VersionKind.REMIX: "Remix",
    VersionKind.VIP: "VIP Mix",
    VersionKind.BOOTLEG: "Bootleg",
    VersionKind.EDIT: "Edit",
    VersionKind.RADIO_EDIT: "Radio Edit",
    VersionKind.EXTENDED: "Extended Mix",
    VersionKind.CLUB: "Club Mix",
    VersionKind.DUB: "Dub",
    VersionKind.LIVE: "Live-Version",
    VersionKind.COVER: "Cover",
    VersionKind.ACOUSTIC: "Akustikversion",
    VersionKind.INSTRUMENTAL: "Instrumental",
    VersionKind.ACAPELLA: "Acapella",
    VersionKind.REMASTER: "Remaster",
    VersionKind.RERECORDED: "Neuaufnahme",
    VersionKind.DEMO: "Demo",
    VersionKind.MASHUP: "Mashup",
    VersionKind.SPED_UP: "Sped up",
    VersionKind.SLOWED: "Slowed",
    VersionKind.NIGHTCORE: "Nightcore",
    VersionKind.KARAOKE: "Karaoke",
}

#: Versionen, die eine Bearbeitung durch eine andere Person bezeichnen (Remixer wichtig)
_PERSONAL = frozenset(
    {VersionKind.REMIX, VersionKind.BOOTLEG, VersionKind.EDIT, VersionKind.VIP, VersionKind.DUB}
)
#: Diese Versionen tauchen im Titel praktisch immer auf – fehlt die Angabe auf der
#: anderen Seite, ist es fast sicher ein anderer Track.
_ALWAYS_LABELLED = frozenset(VersionKind) - {
    VersionKind.RADIO_EDIT,
    VersionKind.EXTENDED,
    VersionKind.CLUB,
    VersionKind.EDIT,
    VersionKind.REMASTER,
}

# Reihenfolge ist wichtig: Spezielles vor Allgemeinem; Treffer werden aus dem Text entfernt.
_VERSION_RULES: tuple[tuple[re.Pattern[str], VersionKind | None], ...] = (
    (re.compile(r"\bkaraoke\b"), VersionKind.KARAOKE),
    (re.compile(r"\bnightcore\b"), VersionKind.NIGHTCORE),
    (re.compile(r"\b(?:sped|speed)\s*up\b"), VersionKind.SPED_UP),
    (re.compile(r"\bslowed(?:\s*(?:and|\+)?\s*reverb)?\b"), VersionKind.SLOWED),
    (re.compile(r"\ba\s*cap+ella\b|\bacapella\b"), VersionKind.ACAPELLA),
    (re.compile(r"\binstrumental\b"), VersionKind.INSTRUMENTAL),
    (re.compile(r"\bacoustic\b|\bunplugged\b|\bakustik\b"), VersionKind.ACOUSTIC),
    (re.compile(r"\blive\b(?:\s+(?:at|from|in|on|version|session)\b.*)?"), VersionKind.LIVE),
    (re.compile(r"\bcover\b"), VersionKind.COVER),
    (re.compile(r"\bdemo\b"), VersionKind.DEMO),
    (re.compile(r"\bmash\s*-?\s*up\b"), VersionKind.MASHUP),
    (re.compile(r"\b\w+s version\b"), VersionKind.RERECORDED),
    (
        re.compile(r"\b(?:\d{4}\s+)?remaster(?:ed)?(?:\s+(?:version|\d{4}))*\b"),
        VersionKind.REMASTER,
    ),
    (
        re.compile(r"\b(?:radio|single|short)\s+(?:edit|mix|version|cut)\b"),
        VersionKind.RADIO_EDIT,
    ),
    (re.compile(r"\bbootleg\b"), VersionKind.BOOTLEG),
    (re.compile(r"\bvip(?:\s+mix)?\b"), VersionKind.VIP),
    (re.compile(r"\bclub\s+(?:mix|edit|version)\b"), VersionKind.CLUB),
    (re.compile(r"\bdub(?:\s+mix)?\b"), VersionKind.DUB),
    (re.compile(r"\bextended(?:\s+(?:mix|version|edit))?\b"), VersionKind.EXTENDED),
    (re.compile(r"\b(?:original|album)\s+(?:mix|version)\b|\boriginal\b"), None),
    (
        re.compile(r"\b(?:remix(?:ed)?|rmx|rework|refix|flip|re\s*edit|mix)\b(?:\s+by\b)?"),
        VersionKind.REMIX,
    ),
    (re.compile(r"\bedit\b"), VersionKind.EDIT),
)

_VERSIONISH_TAIL = re.compile(
    r"\b(?:mix|edit|version|remix|rmx|remaster(?:ed)?|vip|bootleg|rework|refix|flip|"
    r"instrumental|acoustic|unplugged|cover|demo|dub|live|sped up|slowed|reverb|nightcore|"
    r"karaoke|acapella|mashup)(?:\s+\d{4})?$|^(?:\d{4}\s+)?remaster(?:ed)?\b|^live\s+(?:at|from|in|on)\b"
)
_REMIXER_STOP = frozenset(
    {"extended", "original", "radio", "club", "official", "the", "a", "by", "s", "version"}
    | {"mix", "remix"}
)


@dataclass(frozen=True, slots=True)
class VersionInfo:
    kinds: frozenset[VersionKind] = frozenset()
    #: normalisierter Name des Remixers/Bearbeiters, falls angegeben
    remixer: str | None = None
    #: erkannte Originaltexte, für Anzeige und Gründe
    labels: tuple[str, ...] = ()

    @property
    def is_original(self) -> bool:
        return not self.kinds

    @property
    def key(self) -> str:
        kinds = "+".join(sorted(self.kinds))
        return f"{kinds}:{self.remixer}" if self.remixer else kinds

    def describe(self) -> str:
        if self.is_original:
            return "Original"
        names = [VERSION_LABELS[k] for k in sorted(self.kinds)]
        text = " + ".join(names)
        return f"{text} ({self.remixer})" if self.remixer else text


def _parse_version_segment(segment: str) -> tuple[set[VersionKind], str | None, bool]:
    """→ (Versionsarten, Remixer, ob überhaupt eine Versionsangabe erkannt wurde)."""
    text = _clean(fold(segment))
    kinds: set[VersionKind] = set()
    remixer: str | None = None
    recognized = False
    for pattern, kind in _VERSION_RULES:
        match = pattern.search(text)
        if not match:
            continue
        recognized = True
        if kind is not None:
            kinds.add(kind)
            if kind in _PERSONAL and remixer is None:
                remixer = _remixer_from(text, match)
        text = (text[: match.start()] + " " + text[match.end() :]).strip()
    return kinds, remixer, recognized


def _remixer_from(text: str, match: re.Match[str]) -> str | None:
    before = text[: match.start()].split()
    after = text[match.end() :].split()
    # "Remix by X" / "Remixed by X"
    words = after if match.group(0).endswith(" by") or (after and after[0] == "by") else before
    if words and words[0] == "by":
        words = words[1:]
    words = [w for w in words if w not in _REMIXER_STOP and not w.isdigit()]
    name = normalize_artist_name(" ".join(words))
    return name or None


# --- Zerlegen ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reading:
    """Eine Lesart eines Tracks: Titel, Artists, Version."""

    title: str  # normalisierter Basistitel
    artists: frozenset[str]  # Haupt- und Featuring-Artists
    main_artists: frozenset[str]
    feat: frozenset[str]
    version: VersionInfo
    #: unerkannte Klammerzusätze (z. B. „From the Movie …“)
    extras: frozenset[str]
    #: Hinweis auf Videofassung (Official Video …) – Dauer weicht dann oft ab
    video: bool
    #: Lesart aus „Artist - Titel“ im Titelfeld
    embedded_artist: bool = False

    @property
    def compact_title(self) -> str:
        return _compact(self.title)

    @property
    def numbers(self) -> frozenset[str]:
        found = set(re.findall(r"\d+", self.title))
        for extra in self.extras:
            found |= set(re.findall(r"\d+", extra))
        return frozenset(found)


@dataclass(frozen=True, slots=True)
class ParsedTrack:
    facts: TrackFacts
    readings: tuple[Reading, ...]
    matchable: bool

    @property
    def primary(self) -> Reading:
        return self.readings[0]


def _is_noise(segment: str) -> bool:
    text = _clean(fold(segment))
    if not text or _PROD.match(text):
        return True
    return all(word in _NOISE_WORDS for word in text.split())


def _normalize_title_text(value: str) -> str:
    value = re.sub(r"\bpt\b\.?", "part", value)
    return _clean(value)


def parse(facts: TrackFacts) -> ParsedTrack:
    raw = fold(facts.title).strip()
    raw = _PREFIX_NOISE.sub("", raw)
    main_artists, feat = split_artists(facts.artist)
    feat = set(feat)

    kinds: set[VersionKind] = set()
    remixer: str | None = None
    labels: list[str] = []
    extras: set[str] = set()
    video = False

    def take_version(segment: str) -> bool:
        nonlocal remixer
        found, rmx, recognized = _parse_version_segment(segment)
        if recognized:
            kinds.update(found)
            remixer = remixer or rmx
            labels.append(segment.strip())
        return recognized

    if facts.version:
        take_version(facts.version)

    # Klammern (auch mehrfach, von innen nach außen)
    while match := _BRACKET.search(raw):
        segment = match.group(1).strip()
        raw = (raw[: match.start()] + " " + raw[match.end() :]).strip()
        if not segment:
            continue
        if feat_match := _FEAT_BRACKET.match(segment):
            feat |= _split_names(feat_match.group(1))
        elif _is_noise(segment):
            video = video or bool(_VIDEO_HINT.search(segment))
        elif not take_version(segment):
            extras.add(_clean(segment))
    # "Titel | Official Video"
    parts = _PIPE.split(raw)
    raw = parts[0]
    for part in parts[1:]:
        video = video or bool(_VIDEO_HINT.search(part))
    # Featuring im Fließtext
    if feat_match := _FEAT_INLINE.search(raw):
        feat |= _split_names(_DASH.split(feat_match.group(1))[0])
        tail = _DASH.split(feat_match.group(1))[1:]
        raw = " - ".join([raw[: feat_match.start()], *tail])

    # Bindestrich-Segmente: Versionen am Ende abtrennen
    segments = [s.strip() for s in _DASH.split(raw) if s.strip()]
    while len(segments) > 1 and (
        _VERSIONISH_TAIL.search(_clean(segments[-1])) or _is_noise(segments[-1])
    ):
        tail = segments.pop()
        if _is_noise(tail):
            video = video or bool(_VIDEO_HINT.search(tail))
        else:
            take_version(tail)

    version = VersionInfo(frozenset(kinds), remixer, tuple(labels))
    feat_f = frozenset(feat - main_artists)
    full_title = _normalize_title_text(" ".join(segments))

    def reading(title: str, main: frozenset[str], embedded: bool = False) -> Reading:
        return Reading(
            title=title,
            artists=main | feat_f,
            main_artists=main,
            feat=feat_f - main,
            version=version,
            extras=frozenset(extras),
            video=video,
            embedded_artist=embedded,
        )

    readings = [reading(full_title, main_artists)]
    if len(segments) >= 2:
        # "Artist - Titel" (YouTube, SoundCloud-Reposts): zweite Lesart
        head = segments[0]
        rest = _normalize_title_text(" ".join(segments[1:]))
        head_artists = frozenset(_split_names(head))
        if head_artists and rest:
            readings.append(reading(rest, head_artists, embedded=True))
            if head_artists & main_artists or not main_artists:
                # Artist steht doppelt (Feld und Titel): eingebettete Lesart zuerst
                readings.reverse()

    primary = readings[0]
    matchable = bool(primary.title) and not _UNKNOWN_TITLE.match(primary.title)
    matchable = matchable and not (
        all(a in _UNKNOWN_ARTISTS for a in primary.main_artists) or not primary.main_artists
    )
    return ParsedTrack(facts=facts, readings=tuple(readings), matchable=matchable)


def normalize_title(title: str) -> str:
    """Normalisierter Basistitel ohne Zusätze, Featuring und Versionsangaben."""
    parsed = parse(TrackFacts(title=title, artist=""))
    # Ohne Artist-Feld ist bei „A - B“ die eingebettete Lesart die wahrscheinlichere
    return parsed.primary.title


def version_of(title: str, version: str | None = None) -> VersionInfo:
    return parse(TrackFacts(title=title, artist="", version=version)).primary.version


def identity_key(title: str, artist: str, version: str | None) -> str:
    """Exakter Schlüssel aus Basistitel, Artists und Version (für Import-Duplikate)."""
    first = parse(TrackFacts(title=title, artist=artist, version=version)).primary
    artists = "/".join(sorted(first.artists))
    return f"{_compact(first.title)}|{artists}|{first.version.key}"


def is_matchable(facts: TrackFacts) -> bool:
    """Tracks ohne verwertbaren Titel/Artist („Unknown Artist“, „Track 01“) bleiben außen vor."""
    return parse(facts).matchable


# --- Ähnlichkeit ---------------------------------------------------------------------------


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b, autojunk=False).ratio()


@dataclass(frozen=True, slots=True)
class _Sim:
    score: float
    code: str
    text: str


def title_similarity(a: Reading, b: Reading) -> _Sim:
    if a.title == b.title:
        return _Sim(1.0, "title_equal", "Titel identisch")
    if a.compact_title == b.compact_title:
        return _Sim(0.98, "title_spelling", "Titel bis auf Leerzeichen/Satzzeichen gleich")
    ratio = max(_ratio(a.title, b.title), _ratio(a.compact_title, b.compact_title))
    if a.numbers != b.numbers and (a.numbers or b.numbers):
        return _Sim(
            min(ratio, 0.5), "title_numbers", "Titel unterscheiden sich in Zahlen (Teil/Nummer)"
        )
    if ratio >= 0.8:
        return _Sim(ratio, "title_similar", "Titel ähnlich (Schreibweise?)")
    return _Sim(ratio, "title_different", "Titel unterschiedlich")


def artist_similarity(a: Reading, b: Reading) -> _Sim:
    if not a.artists or not b.artists:
        return _Sim(0.0, "artist_missing", "Artist fehlt")
    if a.main_artists == b.main_artists or a.artists == b.artists:
        return _Sim(1.0, "artist_equal", "Artist identisch")
    common = a.artists & b.artists
    if common and (a.main_artists & b.artists or b.main_artists & a.artists):
        jaccard = len(common) / len(a.artists | b.artists)
        return _Sim(
            0.88 + 0.12 * jaccard, "artist_overlap", "Artist stimmt überein, Mitwirkende abweichend"
        )
    compact_a = {_compact(x) for x in a.artists}
    compact_b = {_compact(x) for x in b.artists}
    if compact_a & compact_b:
        return _Sim(0.96, "artist_spelling", "Artist bis auf Schreibweise gleich")
    best = max(_ratio(x, y) for x in compact_a for y in compact_b)
    if best >= 0.8:
        # Bonobo/Bonobos, DJ Snake/DJ Shadow: ähnlich, aber nicht gleich → nur Prüfung
        return _Sim(0.45, "artist_similar_name", "Artist-Namen ähnlich, aber nicht gleich")
    return _Sim(best * 0.4, "artist_different", "Artist unterschiedlich")


# --- Bewertung ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reason:
    code: str
    text: str
    #: Punkte (positiv/negativ); bei ``cap`` die Obergrenze; bei ``info`` 0
    effect: int
    #: plus | minus | info | cap | block
    kind: str

    def as_dict(self) -> dict[str, object]:
        return {"code": self.code, "text": self.text, "effect": self.effect, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class MatchAssessment:
    confidence: int
    level: MatchLevel
    reasons: tuple[Reason, ...]
    #: Gründe, die eine automatische Zusammenführung ausschließen
    blockers: tuple[str, ...]
    title_score: float
    artist_score: float
    duration_diff_ms: int | None
    isrc_match: bool | None
    version_a: str
    version_b: str

    @property
    def auto_merge_allowed(self) -> bool:
        return self.level is MatchLevel.CERTAIN and not self.blockers

    @property
    def is_candidate(self) -> bool:
        return self.level is not MatchLevel.DIFFERENT


def level_for(confidence: int) -> MatchLevel:
    if confidence >= CERTAIN_MIN:
        return MatchLevel.CERTAIN
    if confidence >= LIKELY_MIN:
        return MatchLevel.LIKELY
    if confidence >= REVIEW_MIN:
        return MatchLevel.REVIEW
    return MatchLevel.DIFFERENT


class _Score:
    def __init__(self) -> None:
        self.points = 0.0
        self.caps: list[int] = []
        self.reasons: list[Reason] = []
        self.blockers: list[str] = []

    def add(self, code: str, text: str, points: int) -> None:
        self.points += points
        kind = "plus" if points > 0 else "minus" if points < 0 else "info"
        self.reasons.append(Reason(code, text, points, kind))

    def info(self, code: str, text: str) -> None:
        self.reasons.append(Reason(code, text, 0, "info"))

    def cap(self, code: str, text: str, limit: int) -> None:
        self.caps.append(limit)
        self.reasons.append(Reason(code, text, limit, "cap"))

    def block(self, code: str, text: str) -> None:
        self.blockers.append(code)
        self.reasons.append(Reason(code, text, 0, "block"))


def _best_pair(a: ParsedTrack, b: ParsedTrack) -> tuple[Reading, Reading, _Sim, _Sim]:
    best: tuple[float, Reading, Reading, _Sim, _Sim] | None = None
    for ra in a.readings:
        for rb in b.readings:
            ts, ars = title_similarity(ra, rb), artist_similarity(ra, rb)
            value = TITLE_WEIGHT * ts.score + ARTIST_WEIGHT * ars.score
            # Bei Gleichstand die „normale“ Lesart (Artist aus dem Feld) bevorzugen
            value -= 0.01 * (ra.embedded_artist + rb.embedded_artist)
            if best is None or value > best[0]:
                best = (value, ra, rb, ts, ars)
    assert best is not None
    return best[1], best[2], best[3], best[4]


def _compare_versions(va: VersionInfo, vb: VersionInfo, s: _Score) -> bool:
    """Bewertet die Versionen; ``True``, wenn sie als gleich gelten."""
    if va.kinds == vb.kinds:
        if va.kinds and va.remixer and vb.remixer and _ratio(va.remixer, vb.remixer) < 0.85:
            s.cap(
                "version_remixer",
                f"Unterschiedliche Bearbeiter: {va.describe()} ≠ {vb.describe()}",
                30,
            )
            return False
        if va.kinds & _PERSONAL and bool(va.remixer) != bool(vb.remixer):
            s.cap("version_remixer_unknown", "Bearbeiter nur bei einem Track angegeben", 80)
            s.block("version_remixer_unknown", "Bearbeiter unklar – nicht automatisch")
            return False
        if va.kinds:
            s.info("version_equal", f"Gleiche Version: {va.describe()}")
        else:
            s.info("version_equal", "Beide Original (bzw. ohne Versionsangabe)")
        return True

    only_remaster = (va.kinds ^ vb.kinds) == {VersionKind.REMASTER}
    if only_remaster:
        s.cap(
            "version_remaster", "Remaster gegenüber Original: gleiche Aufnahme, anderes Master", 88
        )
        s.block("version_remaster", "Remaster – nur nach Bestätigung")
        return False
    if not va.kinds or not vb.kinds:
        labelled, plain = (va, vb) if va.kinds else (vb, va)
        if plain.labels:
            # ausdrücklich „Original Mix“/„Album Version“ gegenüber einer anderen Version
            s.cap(
                "version_conflict",
                f"Unterschiedliche Versionen: Original ≠ {labelled.describe()}",
                35,
            )
        elif labelled.kinds & _ALWAYS_LABELLED:
            s.cap(
                "version_conflict",
                f"{labelled.describe()} gegenüber Original – unterschiedliche Versionen",
                40,
            )
        else:
            s.cap(
                "version_one_sided",
                f"Nur ein Track ist als {labelled.describe()} gekennzeichnet",
                75,
            )
            s.block("version_one_sided", "Versionsangabe einseitig – nicht automatisch")
        return False
    s.cap(
        "version_conflict",
        f"Unterschiedliche Versionen: {va.describe()} ≠ {vb.describe()}",
        35,
    )
    return False


def compare(a: TrackFacts | ParsedTrack, b: TrackFacts | ParsedTrack) -> MatchAssessment:
    """Bewertet, ob ``a`` und ``b`` dieselbe Aufnahme in derselben Version sind."""
    pa = a if isinstance(a, ParsedTrack) else parse(a)
    pb = b if isinstance(b, ParsedTrack) else parse(b)
    fa, fb = pa.facts, pb.facts
    s = _Score()

    ra, rb, title_sim, artist_sim = _best_pair(pa, pb)
    s.add(title_sim.code, title_sim.text, round(TITLE_WEIGHT * title_sim.score))
    s.add(artist_sim.code, artist_sim.text, round(ARTIST_WEIGHT * artist_sim.score))
    if ra.embedded_artist or rb.embedded_artist:
        s.info("embedded_artist", "Artist aus dem Titel gelesen („Artist - Titel“)")
    if title_sim.code == "title_different":
        s.cap("title_different", "Titel unterschiedlich – anderer Track", 50)
    elif title_sim.code == "title_similar" and title_sim.score < 0.95:
        s.cap("title_similar", "Titel nur ähnlich – bitte prüfen", 84)
    if artist_sim.code in {"artist_different", "artist_missing"}:
        s.cap("artist_different", "Artist unterschiedlich – anderer Track", 50)
    if title_sim.code == "title_numbers":
        s.cap("title_numbers", "Unterschiedliche Teil-/Nummernangabe im Titel", 45)
    if artist_sim.code == "artist_similar_name":
        s.cap("artist_similar_name", "Nur ähnlicher Artist-Name – könnte ein anderer sein", 70)
        s.block("artist_similar_name", "Artist nicht eindeutig")

    if not (pa.matchable and pb.matchable):
        s.cap("unknown_metadata", "Titel oder Artist unbekannt – keine Zuordnung möglich", 0)

    versions_equal = _compare_versions(ra.version, rb.version, s)

    # Zusätze, die keine bekannte Version sind (z. B. „From the Movie …“)
    if ra.extras != rb.extras:
        diff = sorted(ra.extras ^ rb.extras)
        s.add("extras_differ", f"Zusatz nur bei einem Track: {', '.join(diff)}", -4)

    # Featuring: nur ein Abzug, wenn beide Seiten Mitwirkende nennen und sie sich widersprechen
    if ra.feat and rb.feat and not (ra.feat & rb.feat):
        s.add("feat_conflict", "Unterschiedliche Featuring-Artists", -10)

    # ISRC
    isrc_match: bool | None = None
    if fa.isrc and fb.isrc:
        isrc_match = fa.isrc.upper() == fb.isrc.upper()
        if isrc_match:
            s.add("isrc_equal", "Gleiche ISRC", 30)
        else:
            s.add("isrc_different", "Unterschiedliche ISRC", -10)
            s.cap("isrc_different", "Unterschiedliche ISRC – höchstens Prüfung", 84)
    else:
        s.info("isrc_missing", "ISRC fehlt bei mindestens einem Track")

    # Dauer
    duration_diff: int | None = None
    if fa.duration_ms and fb.duration_ms:
        duration_diff = abs(fa.duration_ms - fb.duration_ms)
        seconds = duration_diff / 1000
        video = ra.video or rb.video
        if seconds <= 2:
            s.add("duration_equal", f"Dauer gleich (±{seconds:.0f} s)", 4)
        elif seconds <= 5:
            s.add("duration_close", f"Dauer weicht {seconds:.0f} s ab", 1)
        elif seconds <= 15:
            s.add("duration_differs", f"Dauer weicht {seconds:.0f} s ab", -5)
        elif seconds <= 30:
            s.add("duration_differs", f"Dauer weicht {seconds:.0f} s ab", -12)
            s.cap("duration_differs", "Dauer weicht deutlich ab", 84)
        elif video and seconds <= 90:
            s.add("duration_video", f"Dauer weicht {seconds:.0f} s ab (Videofassung)", -8)
            s.cap("duration_video", "Videofassung mit abweichender Länge", 84)
        else:
            s.add("duration_conflict", f"Dauer weicht {seconds:.0f} s ab", -25)
            s.cap("duration_conflict", "Tracklänge passt nicht zur selben Version", 60)
    else:
        s.info("duration_missing", "Dauer fehlt bei mindestens einem Track")

    # Veröffentlichungsinformationen
    if fa.album and fb.album and _clean(fold(fa.album)) == _clean(fold(fb.album)):
        s.add("album_equal", "Gleiches Album", 2)
    if fa.release_date and fb.release_date:
        years = abs(fa.release_date.year - fb.release_date.year)
        if years == 0:
            s.add("release_equal", "Gleiches Veröffentlichungsjahr", 2)
        elif years > 1:
            s.add("release_differs", f"Veröffentlichung {years} Jahre auseinander", -6)

    # Plattform-Metadaten
    same_platform_id = False
    for platform in sorted(set(fa.platforms) & set(fb.platforms)):
        label = PLATFORM_LABELS.get(platform, platform)
        if fa.platforms[platform] == fb.platforms[platform]:
            same_platform_id = True
            s.add("platform_same_id", f"Gleicher {label}-Track", 30)
        else:
            s.add("platform_conflict", f"Beide haben einen eigenen {label}-Track", -5)
            s.block(
                "platform_conflict",
                f"Unterschiedliche {label}-Tracks – Zusammenführung nur manuell",
            )
    if fa.platforms and fb.platforms and not set(fa.platforms) & set(fb.platforms):
        s.info("platforms_complement", "Quellen ergänzen sich (verschiedene Plattformen)")

    # Gesamtwert, Obergrenzen, harter Beleg für „sicher“
    exact = (
        title_sim.code == "title_equal"
        and artist_sim.score >= 0.96
        and versions_equal
        and not ra.extras ^ rb.extras
    )
    hard_evidence = (bool(isrc_match) and versions_equal) or same_platform_id
    hard_evidence = hard_evidence or (exact and duration_diff is not None and duration_diff <= 2000)
    if isrc_match and title_sim.score < 0.8:
        s.cap("isrc_metadata_conflict", "Gleiche ISRC, aber Titel passt nicht", 80)
        s.block("isrc_metadata_conflict", "ISRC und Metadaten widersprechen sich")
    if not hard_evidence:
        s.cap(
            "no_hard_evidence",
            "Kein harter Beleg (ISRC oder exakte Daten mit gleicher Dauer) – max. wahrscheinlich",
            CERTAIN_MIN - 1,
        )
    if s.blockers:
        s.cap("blocked", "Automatische Zusammenführung ausgeschlossen", CERTAIN_MIN - 1)

    confidence = max(0, min(100, round(s.points)))
    if s.caps:
        confidence = min(confidence, *s.caps)
    level = level_for(confidence)
    # Nur wirksame Caps als Grund anzeigen (die niedrigste), übrige als Info belassen
    reasons = _tidy_reasons(s.reasons, round(s.points))
    return MatchAssessment(
        confidence=confidence,
        level=level,
        reasons=tuple(reasons),
        blockers=tuple(dict.fromkeys(s.blockers)),
        title_score=round(title_sim.score, 3),
        artist_score=round(artist_sim.score, 3),
        duration_diff_ms=duration_diff,
        isrc_match=isrc_match,
        version_a=ra.version.describe(),
        version_b=rb.version.describe(),
    )


def _tidy_reasons(reasons: list[Reason], points: int) -> list[Reason]:
    """Obergrenzen nur anzeigen, wenn sie den Wert tatsächlich begrenzt haben."""
    return [r for r in reasons if r.kind != "cap" or r.effect < min(points, 100)]
