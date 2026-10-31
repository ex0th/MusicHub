from musichub.domain.types import MatchMethod
from musichub.matching.engine import (
    TrackFingerprint,
    normalize_artists,
    normalize_title,
    rank,
    score,
    version_markers,
)


def fp(
    title: str, artist: str, duration_ms: int | None = None, isrc: str | None = None
) -> TrackFingerprint:
    return TrackFingerprint(title=title, artist=artist, duration_ms=duration_ms, isrc=isrc)


def test_normalize_title_strips_noise_and_features() -> None:
    assert normalize_title("Blinding Lights (Official Video) [HD]") == "blinding lights"
    assert normalize_title("Señorita feat. Camila Cabello") == "senorita"
    assert normalize_title("The Weeknd - Blinding Lights") == "blinding lights"


def test_normalize_artists_splits_collaborations() -> None:
    assert normalize_artists("Shawn Mendes & Camila Cabello") == {"shawn mendes", "camila cabello"}
    assert normalize_artists("The Weeknd - Topic") == {"the weeknd"}


def test_isrc_match_wins() -> None:
    result = score(
        fp("A", "B", isrc="USUM71900001"), fp("Totally different", "X", isrc="usum71900001")
    )
    assert result.score == 1.0
    assert result.method is MatchMethod.ISRC
    assert result.auto_link


def test_youtube_style_title_matches_original() -> None:
    local = fp("Blinding Lights", "The Weeknd", 200_040)
    remote = fp("The Weeknd - Blinding Lights (Official Audio)", "The Weeknd - Topic", 201_000)
    result = score(local, remote)
    assert result.method is MatchMethod.FUZZY
    assert result.auto_link, result


def test_remix_is_never_auto_linked_to_original() -> None:
    local = fp("Blinding Lights", "The Weeknd", 200_000)
    remix = fp("Blinding Lights (Chromatics Remix)", "The Weeknd", 201_000)
    assert version_markers(remix.title) == {"remix"}
    assert not score(local, remix).auto_link


def test_duration_mismatch_lowers_score() -> None:
    local = fp("Song", "Artist", 180_000)
    same = score(local, fp("Song", "Artist", 181_000))
    far = score(local, fp("Song", "Artist", 260_000))
    assert same.score > far.score


def test_rank_filters_and_sorts() -> None:
    local = fp("Blinding Lights", "The Weeknd", 200_000)
    candidates = [
        fp("Something Else", "Other Artist", 150_000),
        fp("Blinding Lights", "The Weeknd", 200_500),
        fp("Blinding Lights - Live", "The Weeknd", 230_000),
    ]
    ranked = rank(local, candidates)
    assert ranked[0][0] == 1
    assert all(i != 0 for i, _ in ranked)
