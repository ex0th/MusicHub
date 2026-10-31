"""Matching-Engine: realistische Fälle aus Spotify, SoundCloud, YouTube und Importen.

Schwerpunkt: Falsche automatische Zusammenführungen dürfen nicht vorkommen. Jeder Fall
in ``MUST_NOT_AUTO_MERGE`` ist ein anderer Track bzw. eine andere Version und darf weder
„sicher“ eingestuft noch automatisch zusammengeführt werden.
"""

from __future__ import annotations

from datetime import date

import pytest

from musichub.domain.types import MatchLevel
from musichub.matching.blocking import MAX_BUCKET, candidate_pairs
from musichub.matching.engine import (
    TrackFacts,
    VersionKind,
    compare,
    identity_key,
    is_matchable,
    normalize_artists,
    normalize_title,
    parse,
    version_of,
)

L = MatchLevel


def t(
    title: str,
    artist: str,
    duration: float | None = None,
    *,
    isrc: str | None = None,
    version: str | None = None,
    album: str | None = None,
    released: str | None = None,
    **platforms: str,
) -> TrackFacts:
    return TrackFacts(
        title=title,
        artist=artist,
        version=version,
        duration_ms=round(duration * 1000) if duration else None,
        isrc=isrc,
        album=album,
        release_date=date.fromisoformat(released) if released else None,
        platforms=platforms,
    )


def codes(a: TrackFacts, b: TrackFacts) -> set[str]:
    return {r.code for r in compare(a, b).reasons}


# --- Normalisierung --------------------------------------------------------------------


def test_normalize_title_strips_noise_features_and_versions() -> None:
    assert normalize_title("Blinding Lights (Official Video) [HD]") == "blinding lights"
    assert normalize_title("Señorita feat. Camila Cabello") == "senorita"
    assert normalize_title("The Weeknd - Blinding Lights") == "blinding lights"
    assert normalize_title("Innerbloom - Extended Mix") == "innerbloom"
    assert normalize_title("Strobe (Original Mix) [Free Download]") == "strobe"
    assert normalize_title("PREMIERE: Strobe | Official Video") == "strobe"
    assert normalize_title("Don't Stop Me Now - Remastered 2011") == "dont stop me now"


def test_normalize_artists_splits_collaborations_and_channel_noise() -> None:
    assert normalize_artists("Shawn Mendes & Camila Cabello") == {"shawn mendes", "camila cabello"}
    assert normalize_artists("The Weeknd - Topic") == {"weeknd"}
    assert normalize_artists("RÜFÜS DU SOL") == normalize_artists("Rufus Du Sol")
    assert normalize_artists("Calvin Harris feat. Rihanna") == {"calvin harris", "rihanna"}
    assert normalize_artists("Disclosure x Sam Smith") == {"disclosure", "sam smith"}


@pytest.mark.parametrize(
    ("title", "version", "kinds", "remixer"),
    [
        ("Song (Original Mix)", None, set(), None),
        ("Song", "Original Mix", set(), None),
        ("Song (Album Version)", None, set(), None),
        ("Song (Chromatics Remix)", None, {VersionKind.REMIX}, "chromatics"),
        ("Song - Tiësto Extended Remix", None, {VersionKind.REMIX, VersionKind.EXTENDED}, "tiesto"),
        ("Song (Remix by Kaytranada)", None, {VersionKind.REMIX}, "kaytranada"),
        ("Song (Skrillex Bootleg)", None, {VersionKind.BOOTLEG}, "skrillex"),
        ("Song (VIP Mix)", None, {VersionKind.VIP}, None),
        ("Song - Radio Edit", None, {VersionKind.RADIO_EDIT}, None),
        ("Song (Extended Mix)", None, {VersionKind.EXTENDED}, None),
        ("Song (Live at Wembley)", None, {VersionKind.LIVE}, None),
        ("Song - Live", None, {VersionKind.LIVE}, None),
        ("Song (Acoustic)", None, {VersionKind.ACOUSTIC}, None),
        ("Song (Sped Up)", None, {VersionKind.SPED_UP}, None),
        ("Song (Slowed + Reverb)", None, {VersionKind.SLOWED}, None),
        ("Song - Remastered 2011", None, {VersionKind.REMASTER}, None),
        ("Song (Taylor's Version)", None, {VersionKind.RERECORDED}, None),
        ("Song (Cover)", None, {VersionKind.COVER}, None),
        ("Song (Instrumental)", None, {VersionKind.INSTRUMENTAL}, None),
    ],
)
def test_version_detection(
    title: str, version: str | None, kinds: set[VersionKind], remixer: str | None
) -> None:
    info = version_of(title, version)
    assert info.kinds == kinds
    assert info.remixer == remixer


def test_title_words_are_not_mistaken_for_versions() -> None:
    # „Live Forever“ ist ein Titel, keine Live-Version
    parsed = parse(t("Oasis - Live Forever", "Oasis"))
    assert parsed.primary.title == "live forever"
    assert parsed.primary.version.is_original
    assert version_of("Mixed Signals").is_original
    assert version_of("Remixes Are Fun").is_original


def test_identity_key_handles_versions() -> None:
    base = identity_key("Innerbloom", "RÜFÜS DU SOL", "Extended Mix")
    assert identity_key("Innerbloom (Extended Mix)", "Rufus du Sol", None) == base
    assert identity_key("Innerbloom - Extended Mix", "RÜFÜS DU SOL", None) == base
    assert identity_key("Innerbloom", "RÜFÜS DU SOL", None) != base
    assert identity_key("Song", "A", "Original Mix") == identity_key("Song", "A", None)
    assert identity_key("Song", "A", "Disclosure Remix") != identity_key("Song", "A", "Other Remix")


def test_unknown_tracks_are_not_matchable() -> None:
    assert not is_matchable(t("Track 01", "Unknown Artist"))
    assert not is_matchable(t("Intro", "Various Artists"))
    assert not is_matchable(t("Untitled", "Burial"))
    assert is_matchable(t("Archangel", "Burial"))
    result = compare(t("Track 01", "Unknown Artist", 200), t("Track 01", "Unknown Artist", 200))
    assert result.level is L.DIFFERENT
    assert not result.auto_merge_allowed


# --- Identische Tracks ---------------------------------------------------------------------

SAME = [
    # (Beschreibung, a, b, mindestens erwartete Stufe)
    (
        "Spotify Original Mix vs. SoundCloud ohne Zusatz, gleiche Dauer",
        t("Track Name (Original Mix)", "Artist", 312.4),
        t("Track Name", "Artist", 313.1),
        L.CERTAIN,
    ),
    (
        "Spotify vs. YouTube „Artist - Titel [Official Audio]“ auf Topic-Kanal",
        t("Track Name (Original Mix)", "Artist", 312.4),
        t("Artist - Track Name [Official Audio]", "Artist - Topic", 312.9),
        L.CERTAIN,
    ),
    (
        "YouTube-Upload auf fremdem Kanal",
        t("Kerala", "Bonobo", 230.0),
        t("Bonobo - Kerala (Official Video)", "Ninja Tune", 231.0),
        L.CERTAIN,
    ),
    (
        "Gleiche ISRC trotz anderer Schreibweise des Artists",
        t("Blinding Lights", "The Weeknd", isrc="USUG11904206"),
        t("Blinding Lights", "Weeknd", isrc="usug11904206"),
        L.CERTAIN,
    ),
    (
        "Versionsfeld vs. Version im Titel",
        t("Innerbloom", "RÜFÜS DU SOL", 576, version="Extended Mix"),
        t("Innerbloom - Extended Mix", "Rufus Du Sol", 577),
        L.CERTAIN,
    ),
    (
        "Gleicher Remix auf zwei Plattformen",
        t("Midnight City (Eric Prydz Remix)", "M83", 476),
        t("M83 - Midnight City (Eric Prydz Remix)", "Eric Prydz", 476.5),
        L.CERTAIN,
    ),
]


@pytest.mark.parametrize(("label", "a", "b", "level"), SAME, ids=[s[0] for s in SAME])
def test_identical_tracks(label: str, a: TrackFacts, b: TrackFacts, level: MatchLevel) -> None:
    result = compare(a, b)
    assert result.level is level, (label, result)
    # symmetrisch
    assert compare(b, a).confidence == result.confidence


# --- Unterschiedliche Schreibweisen --------------------------------------------------------

SPELLING = [
    (
        "Groß-/Kleinschreibung",
        t("BLINDING LIGHTS", "THE WEEKND", 200),
        t("Blinding Lights", "The Weeknd", 200),
    ),
    (
        "Akzente",
        t("Señorita", "Shawn Mendes & Camila Cabello", 191),
        t("Senorita", "Shawn Mendes, Camila Cabello", 191),
    ),
    ("Apostroph", t("Don't Start Now", "Dua Lipa", 183), t("Dont Start Now", "Dua Lipa", 183)),
    ("Leerzeichen", t("Inner Bloom", "Rufus Du Sol", 300), t("Innerbloom", "RÜFÜS DU SOL", 300)),
    ("Artikel „The“", t("Wonderwall", "The Oasis", 258), t("Wonderwall", "Oasis", 258)),
    ("&/and", t("Rhythm & Blues", "Artist", 200), t("Rhythm and Blues", "Artist", 200)),
    ("Pt./Part", t("Opus Pt. 2", "Eric Prydz", 540), t("Opus Part 2", "Eric Prydz", 540)),
    (
        "Featuring im Titel vs. Artist-Feld",
        t("Señorita (feat. Camila Cabello)", "Shawn Mendes", 191),
        t("Señorita", "Shawn Mendes & Camila Cabello", 191),
    ),
    (
        "VEVO-Kanal",
        t("Shake It Off", "TaylorSwiftVEVO", 242),
        t("Shake It Off", "Taylor Swift", 242),
    ),
]


@pytest.mark.parametrize(("label", "a", "b"), SPELLING, ids=[s[0] for s in SPELLING])
def test_spelling_variants_are_recognized(label: str, a: TrackFacts, b: TrackFacts) -> None:
    result = compare(a, b)
    assert result.level in {L.CERTAIN, L.LIKELY}, (label, result)


def test_typo_in_title_is_likely_not_certain() -> None:
    result = compare(t("Midnight Citty", "M83", 244), t("Midnight City", "M83", 244))
    assert result.level is L.LIKELY
    assert not result.auto_merge_allowed


# --- Darf NIE automatisch zusammengeführt werden ---------------------------------------------

MUST_NOT_AUTO_MERGE = [
    # Versionen (gleiche Dauer, damit nur die Versionsregel greift)
    (
        "Original vs. Remix",
        t("Strobe", "deadmau5", 600),
        t("Strobe (Kaskade Remix)", "deadmau5", 600),
    ),
    (
        "Original Mix vs. VIP Mix",
        t("Track Name (Original Mix)", "Artist", 300),
        t("Track Name (VIP Mix)", "Artist", 300),
    ),
    ("Original vs. Bootleg", t("Song", "Artist", 210), t("Song (Skrillex Bootleg)", "Artist", 210)),
    (
        "Remix vs. anderer Remix",
        t("Midnight City (Eric Prydz Remix)", "M83", 476),
        t("Midnight City (Trentemøller Remix)", "M83", 476),
    ),
    (
        "Remix vs. Bootleg desselben Bearbeiters",
        t("Song (Skrillex Remix)", "Artist", 250),
        t("Song (Skrillex Bootleg)", "Artist", 250),
    ),
    (
        "Radio Edit vs. Extended Mix",
        t("Song - Radio Edit", "Artist", 200),
        t("Song (Extended Mix)", "Artist", 200),
    ),
    (
        "Original Mix vs. Extended Mix",
        t("Song (Original Mix)", "Artist", 360),
        t("Song (Extended Mix)", "Artist", 360),
    ),
    ("Original vs. Radio Edit", t("Song", "Artist", 240), t("Song - Radio Edit", "Artist", 240)),
    (
        "Studio vs. Live",
        t("Wonderwall", "Oasis", 258),
        t("Wonderwall (Live at Knebworth)", "Oasis", 258),
    ),
    (
        "Studio vs. Live (Bindestrich)",
        t("Wonderwall", "Oasis", 258),
        t("Wonderwall - Live", "Oasis", 258),
    ),
    (
        "Original vs. Cover",
        t("Hurt", "Nine Inch Nails", 373),
        t("Hurt (Cover)", "Nine Inch Nails", 373),
    ),
    ("Original vs. Akustik", t("Song", "Artist", 200), t("Song (Acoustic)", "Artist", 200)),
    ("Original vs. Sped Up", t("Song", "Artist", 200), t("Song (Sped Up)", "Artist", 200)),
    (
        "Original vs. Instrumental",
        t("Song", "Artist", 200),
        t("Song (Instrumental)", "Artist", 200),
    ),
    ("Original vs. Remaster", t("Song", "Queen", 200), t("Song - Remastered 2011", "Queen", 200)),
    (
        "Original vs. Neuaufnahme",
        t("Love Story", "Taylor Swift", 235),
        t("Love Story (Taylor's Version)", "Taylor Swift", 235),
    ),
    # Künstler
    ("Ähnlicher Artist Bonobo/Bonobos", t("Kerala", "Bonobo", 230), t("Kerala", "Bonobos", 230)),
    (
        "Ähnlicher Artist DJ Snake/DJ Shadow",
        t("Midnight", "DJ Snake", 200),
        t("Midnight", "DJ Shadow", 200),
    ),
    (
        "Anderer Artist, gleicher Titel",
        t("Hurt", "Nine Inch Nails", 373),
        t("Hurt", "Johnny Cash", 218),
    ),
    ("Anderer Artist, gleicher Titel und Dauer", t("Intro", "The xx", 128), t("Intro", "M83", 128)),
    # Titel
    ("Teil 1 vs. Teil 2", t("Opus Part 1", "Eric Prydz", 540), t("Opus Part 2", "Eric Prydz", 540)),
    ("Ähnlicher, anderer Titel", t("Strobe", "deadmau5", 600), t("Strobes", "deadmau5", 600)),
    # Daten
    (
        "Unterschiedliche ISRC",
        t("Song", "Artist", 200, isrc="GBAAA0000001"),
        t("Song", "Artist", 200, isrc="GBAAA0000002"),
    ),
    ("Dauer weicht 60 s ab", t("Song", "Artist", 200), t("Song", "Artist", 260)),
    ("Dauer weicht 20 s ab", t("Song", "Artist", 200), t("Song", "Artist", 220)),
    ("Ohne Dauer und ISRC", t("Song", "Artist"), t("Song", "Artist")),
    (
        "Gleiche ISRC, widersprüchlicher Titel",
        t("Song", "Artist", isrc="GBAAA0000001"),
        t("Completely Else", "Artist", isrc="GBAAA0000001"),
    ),
    (
        "Zwei verschiedene Spotify-Tracks",
        t("Song", "Artist", 200, spotify="aaa"),
        t("Song", "Artist", 200, spotify="bbb"),
    ),
    (
        "Remix ohne Bearbeiter vs. Remix mit",
        t("Song (Remix)", "Artist", 200),
        t("Song (Kaskade Remix)", "Artist", 200),
    ),
    (
        "Unbekannter Track",
        t("Track 01", "Unknown Artist", 200),
        t("Track 01", "Unknown Artist", 200),
    ),
]


@pytest.mark.parametrize(
    ("label", "a", "b"), MUST_NOT_AUTO_MERGE, ids=[s[0] for s in MUST_NOT_AUTO_MERGE]
)
def test_must_not_auto_merge(label: str, a: TrackFacts, b: TrackFacts) -> None:
    for x, y in ((a, b), (b, a)):
        result = compare(x, y)
        assert result.level is not L.CERTAIN, (label, result)
        assert not result.auto_merge_allowed, (label, result)
        assert result.confidence < 95, (label, result)


VERSION_CONFLICTS = MUST_NOT_AUTO_MERGE[:14]


@pytest.mark.parametrize(
    ("label", "a", "b"), VERSION_CONFLICTS, ids=[s[0] for s in VERSION_CONFLICTS]
)
def test_clear_version_conflicts_are_different_tracks(
    label: str, a: TrackFacts, b: TrackFacts
) -> None:
    """Klar gekennzeichnete andere Versionen werden gar nicht erst vorgeschlagen –
    Ausnahme: Radio Edit/Extended Mix gegenüber unbeschriftet (oft nur fehlende Angabe)."""
    result = compare(a, b)
    if label == "Original vs. Radio Edit":
        assert result.level is L.REVIEW
    else:
        assert result.level is L.DIFFERENT, (label, result)


@pytest.mark.parametrize(
    ("a", "b"),
    [
        (t("Something Else", "The Weeknd", 200), t("Blinding Lights", "The Weeknd", 200)),
        (t("Hurt", "Johnny Cash", 218), t("Hurt", "Nine Inch Nails", 218)),
        (t("Intro", "The xx", 128), t("Intro", "M83", 128)),
    ],
)
def test_different_title_or_artist_is_not_suggested(a: TrackFacts, b: TrackFacts) -> None:
    assert compare(a, b).level is L.DIFFERENT


def test_near_title_needs_review() -> None:
    assert compare(t("Strobe", "deadmau5", 600), t("Strobes", "deadmau5", 600)).level is L.REVIEW


def test_similar_artist_needs_manual_review() -> None:
    result = compare(t("Kerala", "Bonobo", 230), t("Kerala", "Bonobos", 230))
    assert result.level is L.REVIEW
    assert "artist_similar_name" in result.blockers


def test_remaster_is_likely_but_never_automatic() -> None:
    result = compare(
        t("Bohemian Rhapsody", "Queen", 355, isrc="GBUM71029604"),
        t("Bohemian Rhapsody - Remastered 2011", "Queen", 355, isrc="GBUM71029604"),
    )
    assert result.level is L.LIKELY
    assert "version_remaster" in result.blockers


# --- Fehlende ISRC, abweichende Längen -------------------------------------------------------


def test_missing_isrc_needs_duration_for_certainty() -> None:
    without_duration = compare(t("Song", "Artist"), t("Song", "Artist"))
    assert without_duration.level is L.LIKELY
    assert without_duration.confidence == 94
    assert "no_hard_evidence" in codes(t("Song", "Artist"), t("Song", "Artist"))
    with_duration = compare(t("Song", "Artist", 200), t("Song", "Artist", 201.5))
    assert with_duration.level is L.CERTAIN


def test_missing_isrc_on_one_side_is_neutral() -> None:
    result = compare(t("Song", "Artist", 200, isrc="GBAAA0000001"), t("Song", "Artist", 200))
    assert result.level is L.CERTAIN
    assert result.isrc_match is None


@pytest.mark.parametrize(
    ("diff", "level"),
    [
        (0, L.CERTAIN),
        (2, L.CERTAIN),
        (4, L.LIKELY),
        (10, L.LIKELY),
        (20, L.REVIEW),
        (60, L.REVIEW),
        (120, L.REVIEW),
    ],
)
def test_duration_deviation(diff: int, level: MatchLevel) -> None:
    result = compare(t("Song", "Artist", 200), t("Song", "Artist", 200 + diff))
    assert result.level is level, result


def test_long_duration_gap_caps_even_with_matching_isrc_absent() -> None:
    result = compare(t("Song", "Artist", 200), t("Song", "Artist", 320))
    assert result.confidence <= 60


def test_video_version_tolerates_longer_runtime() -> None:
    result = compare(
        t("Blinding Lights", "The Weeknd", 200),
        t("The Weeknd - Blinding Lights (Official Video)", "TheWeekndVEVO", 262),
    )
    assert result.level is L.REVIEW
    assert "duration_video" in {r.code for r in result.reasons}


def test_isrc_overrides_small_metadata_differences() -> None:
    result = compare(
        t("Blinding Lights", "The Weeknd", 200, isrc="USUG11904206"),
        t("Blinding Lights", "The Weeknd", 207, isrc="USUG11904206"),
    )
    assert result.level is L.CERTAIN


# --- Veröffentlichung und Plattform ------------------------------------------------------------


def test_release_information_adjusts_score() -> None:
    same = compare(
        t("Song", "Artist", 200, album="LP", released="2019-03-01"),
        t("Song", "Artist", 200, album="lp", released="2019-11-01"),
    )
    far = compare(
        t("Song", "Artist", 200, released="2001-01-01"),
        t("Song", "Artist", 200, released="2019-01-01"),
    )
    assert {"album_equal", "release_equal"} <= {r.code for r in same.reasons}
    assert "release_differs" in {r.code for r in far.reasons}
    assert far.confidence < same.confidence


def test_same_platform_id_is_certain_complementary_platforms_are_fine() -> None:
    assert compare(t("Song", "Artist", spotify="x1"), t("Song", "Artist", spotify="x1")).level is (
        L.CERTAIN
    )
    result = compare(t("Song", "Artist", 200, spotify="x1"), t("Song", "Artist", 200, youtube="y"))
    assert result.level is L.CERTAIN
    assert result.auto_merge_allowed


def test_reasons_are_traceable() -> None:
    result = compare(t("Strobe", "deadmau5", 600), t("Strobe (Kaskade Remix)", "deadmau5", 600))
    caps = [r for r in result.reasons if r.kind == "cap"]
    assert any(r.code == "version_conflict" and r.effect == result.confidence for r in caps)
    assert all(set(r.as_dict()) == {"code", "text", "effect", "kind"} for r in result.reasons)
    assert result.version_a == "Original"
    assert result.version_b == "Remix (kaskade)"


# --- Blocking ---------------------------------------------------------------------------------


def test_candidate_pairs_find_variants_but_not_everything() -> None:
    tracks = {
        "1": t("Kerala", "Bonobo"),
        "2": t("Bonobo - Kerala (Official Video)", "Ninja Tune"),
        "3": t("Strobe", "deadmau5"),
        "4": t("Strobe (Kaskade Remix)", "deadmau5"),
        "5": t("Midnight Citty", "M83"),
        "6": t("Midnight City", "M83"),
        "7": t("Something", "Else", isrc="GBAAA0000001"),
        "8": t("Other Title", "Else", isrc="GBAAA0000001"),
        "9": t("Track 01", "Unknown Artist"),
        "10": t("Track 01", "Unknown Artist"),
    }
    pairs = candidate_pairs((k, parse(v)) for k, v in tracks.items())
    assert {("1", "2"), ("3", "4"), ("5", "6"), ("7", "8")} <= pairs
    assert ("10", "9") not in pairs and ("9", "10") not in pairs
    assert ("1", "3") not in pairs


def test_candidate_pairs_skip_oversized_buckets() -> None:
    tracks = [(str(i), parse(t("Intro", f"Artist {i}"))) for i in range(MAX_BUCKET + 1)]
    assert candidate_pairs(tracks) == set()
