"""Dateiprüfung und Import-Adapter (ohne Datenbank)."""

from __future__ import annotations

from pathlib import Path

import pytest

from musichub.transfer.decoding import UploadError, decode_upload, safe_filename
from musichub.transfer.formats import detect_and_parse
from musichub.transfer.matching import LibraryIndex, LibraryTrack, identity_key
from musichub.transfer.records import FormatError, ImportRecord, parse_duration

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "import"
MB = 1024 * 1024


def parse(name: str, content: str | bytes, *, forced: str | None = None, max_rows: int = 1000):  # type: ignore[no-untyped-def]
    data = content.encode() if isinstance(content, str) else content
    return detect_and_parse(
        decode_upload(name, data, max_bytes=MB), max_rows=max_rows, forced_format=forced
    )


# --- Dateiprüfung ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("liste.xlsx", b"PK\x03\x04", "nicht unterstützt"),
        ("liste", b"a,b", "nicht unterstützt"),
        ("leer.csv", b"", "leer"),
        ("bin.csv", b"title,artist\n\x00\x01\x02", "Binärdaten"),
        ("ctrl.txt", b"A - B\x07", "Steuerzeichen"),
        ("nur-leer.txt", b"   \n\n", "keine Daten"),
    ],
)
def test_rejected_files(name: str, data: bytes, message: str) -> None:
    with pytest.raises(UploadError, match=message):
        decode_upload(name, data, max_bytes=MB)


def test_size_limit_is_enforced_on_content() -> None:
    with pytest.raises(UploadError, match="größer"):
        decode_upload("gross.csv", b"x" * (2 * MB), max_bytes=MB)


def test_encodings() -> None:
    utf8_bom = decode_upload("a.csv", "\ufefftitle,artist\nÄ,Ö".encode(), max_bytes=MB)
    assert utf8_bom.text.startswith("title") and utf8_bom.encoding == "utf-8"
    utf16 = decode_upload("a.csv", "title,artist\nÄ,Ö".encode("utf-16"), max_bytes=MB)
    assert "Ä" in utf16.text and utf16.encoding == "utf-16"
    cp1252 = decode_upload("a.csv", "title,artist\nCafé,Zoë".encode("cp1252"), max_bytes=MB)
    assert "Café" in cp1252.text and cp1252.warnings


def test_filenames_are_sanitized() -> None:
    assert safe_filename("../../etc/passwd.csv") == "passwd.csv"
    assert safe_filename("C:\\Users\\x\\liste.csv") == "liste.csv"
    assert safe_filename(None) == "upload"


# --- Formaterkennung anhand der Beispieldateien --------------------------------------


@pytest.mark.parametrize(
    ("file", "fmt", "valid", "invalid", "playlists"),
    [
        ("tunemymusic.csv", "tunemymusic_csv", 5, 0, {"Sonntagabend": 3, "Lauftraining": 2}),
        ("soundiiz.csv", "soundiiz_csv", 3, 0, {}),
        ("exportify.csv", "exportify_csv", 3, 0, {}),
        ("bibliothek.csv", "generic_csv", 4, 1, {"Lauftraining": 1, "Warehouse": 2}),
        ("tracklist.txt", "txt", 4, 2, {}),
        ("tracks.json", "generic_json", 4, 0, {"Warehouse": 3}),
    ],
)
def test_example_files(
    file: str, fmt: str, valid: int, invalid: int, playlists: dict[str, int]
) -> None:
    path = EXAMPLES / file
    result = detect_and_parse(
        decode_upload(path.name, path.read_bytes(), max_bytes=MB), max_rows=1000
    )
    assert result.format == fmt
    assert sum(r.valid for r in result.records) == valid
    assert sum(not r.valid for r in result.records) == invalid
    assert {p.name: len(p.entries) for p in result.playlists} == playlists


def test_tunemymusic_fields() -> None:
    result = parse(
        "x.csv",
        "Track name,Artist name,Album,Playlist name,Type,ISRC,Spotify - id,Deezer - id\n"
        "Teardrop,Massive Attack,Mezzanine,Abend,Playlist,gb-aaa-98-00058,67Hna13dNDkZvBpTXRIaOJ,123\n",
    )
    r = result.records[0]
    assert (r.title, r.artist, r.album, r.isrc) == (
        "Teardrop",
        "Massive Attack",
        "Mezzanine",
        "GBAAA9800058",
    )
    assert [(s.platform, s.external_id) for s in r.sources] == [
        ("spotify", "67Hna13dNDkZvBpTXRIaOJ")
    ]
    assert any("Deezer - id" in w for w in result.warnings)


def test_generic_csv_variants() -> None:
    semicolon = parse("x.csv", "Titel;Interpret;Dauer\nSong;Band;3:05\n")
    assert semicolon.records[0].duration_ms == 185_000
    tab = parse("x.tsv", "title\tartist\tlength\nSong\tBand\t185\n")
    assert tab.records[0].duration_ms == 185_000
    quoted = parse("x.csv", 'title,artist\n"Komma, im Titel","Sänger ""X"""\n')
    assert quoted.records[0].title == "Komma, im Titel"
    assert quoted.records[0].artist == 'Sänger "X"'
    crlf = parse("x.csv", "title,artist\r\nA,B\r\n\r\nC,D\r\n")
    assert [r.title for r in crlf.records] == ["A", "C"]


def test_positions_order_playlist() -> None:
    result = parse(
        "x.csv",
        "title,artist,playlist,position\nC,X,P,3\nA,X,P,1\nB,X,P,2\n",
    )
    assert [result.records[i].title for i in result.playlists[0].entries] == ["A", "B", "C"]


def test_unknown_columns_rejected() -> None:
    with pytest.raises(FormatError, match="Spalten nicht erkannt"):
        parse("x.csv", "foo,bar\n1,2\n")


def test_row_limit() -> None:
    with pytest.raises(FormatError, match="mehr als 2"):
        parse("x.csv", "title,artist\nA,B\nC,D\nE,F\n", max_rows=2)


def test_invalid_rows_do_not_abort() -> None:
    result = parse(
        "x.csv",
        "title,artist,isrc,spotify url\n"
        ",Fehlt Titel,,\n"
        "Gut,Band,KAPUTT,https://example.com/x\n"
        f"{'x' * 600},Band,,\n",
    )
    first, second, third = result.records
    assert first.errors == ["Titel fehlt."]
    assert second.valid and second.isrc is None and len(second.warnings) == 2
    assert not third.valid and "länger als 500" in third.errors[0]


def test_json_variants_and_errors() -> None:
    assert parse("x.json", '[{"title": "A", "artist": "B"}]').format == "generic_json"
    single = parse("x.json", '{"name": "Mix", "tracks": [{"title": "A", "artist": "B"}]}')
    assert single.playlists[0].name == "Mix"
    mixed = parse("x.json", '[{"title": "A", "artist": "B"}, 5, {"title": "C"}]')
    assert [r.valid for r in mixed.records] == [True, False, False]
    with pytest.raises(FormatError, match="Ungültiges JSON"):
        parse("x.json", '{"tracks": [')
    with pytest.raises(FormatError, match="nicht erkannt"):
        parse("x.json", '{"foo": 1}')
    with pytest.raises(FormatError, match="Version"):
        parse("x.json", '{"format": "musichub", "version": 99, "tracks": []}')


def test_forced_format_must_match_file_type() -> None:
    with pytest.raises(FormatError, match="erwartet"):
        parse("x.txt", "A - B", forced="tunemymusic_csv")


def test_txt_parsing_rules() -> None:
    result = parse(
        "x.txt",
        "# Kommentar\n"
        "01. Daft Punk - Around the World [7:09]\n"
        "2) Aphex Twin – Xtal https://youtu.be/dQw4w9WgXcQ\n"
        "99 Luftballons - Nena\n",
    )
    a, b, c = result.records
    assert (a.artist, a.title, a.duration_ms) == ("Daft Punk", "Around the World", 429_000)
    assert (b.artist, b.title, b.sources[0].platform) == ("Aphex Twin", "Xtal", "youtube")
    assert (c.artist, c.title) == ("99 Luftballons", "Nena")  # Nummer ohne Punkt bleibt Teil


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("3:45", "auto", 225_000),
        ("1:02:03", "auto", 3_723_000),
        ("225", "auto", 225_000),
        ("225000", "auto", 225_000),
        ("225000", "ms", 225_000),
        ("abc", "auto", None),
        ("0", "s", None),
    ],
)
def test_duration_parsing(value: str, unit: str, expected: int | None) -> None:
    assert parse_duration(value, ImportRecord(row=1), unit=unit) == expected


# --- Abgleich -------------------------------------------------------------------------


def test_identity_key_handles_versions() -> None:
    base = identity_key("Innerbloom", "RÜFÜS DU SOL", "Extended Mix")
    assert identity_key("Innerbloom (Extended Mix)", "Rufus du Sol", None) == base
    assert identity_key("Innerbloom - Extended Mix", "RÜFÜS DU SOL", None) == base
    assert identity_key("Innerbloom", "RÜFÜS DU SOL", None) != base
    assert identity_key("Song", "A", "Original Mix") == identity_key("Song", "A", None)
    assert identity_key("Song", "A", "Disclosure Remix") != identity_key("Song", "A", "Other Remix")


def test_library_index_priorities() -> None:
    index = LibraryIndex(
        [
            LibraryTrack("t1", "Teardrop", "Massive Attack", isrc="GBAAA9800058"),
            LibraryTrack("t2", "Kerala", "Bonobo", sources={"spotify:6RNUJQCq0x1kFzm8MDeD8U"}),
            LibraryTrack("t3", "Blinding Lights", "The Weeknd", duration_ms=200_000),
        ]
    )

    def rec(**kw):  # type: ignore[no-untyped-def]
        return ImportRecord(row=1, **kw)

    assert index.exact(rec(title="x", artist="y", isrc="GBAAA9800058")).reason == "isrc"  # type: ignore[union-attr]
    from musichub.transfer.records import SourceRef

    by_source = index.exact(
        rec(title="?", artist="?", sources=[SourceRef("spotify", "6RNUJQCq0x1kFzm8MDeD8U", "u")])
    )
    assert by_source is not None and by_source.track_id == "t2"
    assert index.exact(rec(title="teardrop", artist="massive attack")).reason == "metadata"  # type: ignore[union-attr]
    assert index.exact(rec(title="Blinding Light", artist="The Weeknd")) is None
    similar = index.similar(rec(title="Blinding Light", artist="The Weeknd", duration_ms=201_000))
    assert similar is not None and similar.track_id == "t3" and similar.score >= 0.75
    assert index.similar(rec(title="Something else", artist="The Weeknd")) is None
