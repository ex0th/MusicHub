"""Import-Adapter. Jeder Adapter beschreibt genau ein Dateiformat.

Formate, deren Aufbau wir nicht aus einer belastbaren Quelle kennen, werden nicht als
eigenes Format geraten, sondern über den generischen CSV-/JSON-Adapter mit
dokumentierten Spaltennamen gelesen (siehe docs/IMPORT_EXPORT.md).
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from musichub.domain.platform_urls import PlatformUrlError, canonical_url, validate_external_id
from musichub.domain.types import Platform
from musichub.transfer.formats.csv_common import (
    GENERIC_COLUMNS,
    PLAYLIST_COLUMNS,
    POSITION_COLUMNS,
    CsvTable,
    FieldSetter,
    _duration,
    _isrc,
    _release,
    _set,
    _url,
    apply_columns,
    first,
    normalize_header,
    raw_row,
)
from musichub.transfer.records import (
    FormatError,
    ImportRecord,
    ParsedPlaylist,
    ParseResult,
    SourceRef,
    add_url,
    parse_duration,
    parse_isrc,
    parse_release_date,
    parse_uuid,
    split_tags,
    unescape_formula,
)


@dataclass(frozen=True, slots=True)
class FormatInfo:
    id: str
    label: str
    kind: str  # csv | json | txt


FORMATS: dict[str, FormatInfo] = {
    f.id: f
    for f in [
        FormatInfo("musichub_csv", "MusicHub CSV", "csv"),
        FormatInfo("musichub_json", "MusicHub JSON", "json"),
        FormatInfo("tunemymusic_csv", "TuneMyMusic CSV", "csv"),
        FormatInfo("soundiiz_csv", "Soundiiz CSV", "csv"),
        FormatInfo("exportify_csv", "Exportify CSV (Spotify)", "csv"),
        FormatInfo("generic_csv", "CSV (allgemein)", "csv"),
        FormatInfo("generic_json", "JSON (allgemein)", "json"),
        FormatInfo("txt", "Textliste", "txt"),
    ]
}


def _group_playlists(
    placements: Iterable[tuple[int, str, int | None]],
    descriptions: dict[str, str | None] | None = None,
) -> list[ParsedPlaylist]:
    """(Record-Index, Playlist-Name, Position|None) -> Playlists in Dateireihenfolge."""
    groups: dict[str, list[tuple[float, int, int]]] = {}
    for order, (index, name, position) in enumerate(placements):
        key = name.strip()
        if key:
            sort_pos = float(position) if position is not None else float("inf")
            groups.setdefault(key, []).append((sort_pos, order, index))
    return [
        ParsedPlaylist(
            name=name,
            description=(descriptions or {}).get(name),
            entries=[i for _, _, i in sorted(entries)],
        )
        for name, entries in groups.items()
    ]


def _position(value: str, record: ImportRecord) -> int | None:
    if not value:
        return None
    try:
        return int(float(value))
    except ValueError:
        record.warnings.append(
            f"Position „{value}“ ist keine Zahl, Dateireihenfolge wird verwendet."
        )
        return None


def _table_records(
    table: CsvTable,
    columns: dict[str, FieldSetter],
    *,
    playlist_columns: list[str] | None = None,
    unescape: bool = False,
    extra: Any = None,
) -> tuple[list[ImportRecord], list[tuple[int, str, int | None]]]:
    records: list[ImportRecord] = []
    placements: list[tuple[int, str, int | None]] = []
    for line, row in table.rows:
        if unescape:
            row = {k: unescape_formula(v) for k, v in row.items()}
        record = ImportRecord(row=line, raw=raw_row(table, row))
        if row.pop("__extra__", None):
            record.warnings.append("Zeile hat mehr Werte als die Kopfzeile, überzählige ignoriert.")
        apply_columns(record, row, columns)
        if extra:
            extra(record, row)
        records.append(record.finalize())
        if playlist_columns:
            name = first(row, playlist_columns)
            if name:
                placements.append(
                    (len(records) - 1, name, _position(first(row, POSITION_COLUMNS), record))
                )
    return records, placements


# --- MusicHub CSV ----------------------------------------------------------------------

MUSICHUB_CSV_COLUMNS = [
    "musichub_id",
    "title",
    "artist",
    "version",
    "album",
    "duration_ms",
    "isrc",
    "release_date",
    "tags",
    "spotify_url",
    "soundcloud_url",
    "youtube_url",
    "playlist",
    "playlist_position",
    "notes",
]


def parse_musichub_csv(table: CsvTable, filename: str) -> ParseResult:
    columns: dict[str, FieldSetter] = {
        "title": _set("title"),
        "artist": _set("artist"),
        "version": _set("version"),
        "album": _set("album"),
        "duration_ms": _duration("ms"),
        "isrc": _isrc,
        "release_date": _release,
        "spotify_url": _url(Platform.SPOTIFY),
        "soundcloud_url": _url(Platform.SOUNDCLOUD),
        "youtube_url": _url(Platform.YOUTUBE),
        "notes": _set("notes"),
    }

    def extra(record: ImportRecord, row: dict[str, str]) -> None:
        record.musichub_id = parse_uuid(row.get("musichub_id"))
        record.tags.extend(split_tags(row.get("tags", "")))

    records, placements = _table_records(
        table, columns, playlist_columns=["playlist"], unescape=True, extra=extra
    )
    # Positionen stehen in "playlist_position"
    placements = [
        (i, name, _position(table.rows[i][1].get("playlist_position", ""), records[i]))
        for i, name, _ in placements
    ]
    return ParseResult("musichub_csv", records, _group_playlists(placements))


# --- TuneMyMusic CSV -------------------------------------------------------------------
# Spalten laut Dokumentation importierender Dienste: "Track name", "Artist name", "Album",
# "Playlist name", "Type", "ISRC" sowie je Plattform "<Plattform> - id".

_PLATFORM_ID_COLUMN = re.compile(r"^(spotify|youtube|soundcloud|[\w ]+?) - id$")


def parse_tunemymusic_csv(table: CsvTable, filename: str) -> ParseResult:
    columns: dict[str, FieldSetter] = {
        "track name": _set("title"),
        "artist name": _set("artist"),
        "album": _set("album"),
        "isrc": _isrc,
        "spotify - id": _url(Platform.SPOTIFY),
        "youtube - id": _url(Platform.YOUTUBE),
        "soundcloud - id": _url(Platform.SOUNDCLOUD),
    }
    records, placements = _table_records(table, columns, playlist_columns=["playlist name"])
    ignored = sorted(
        h
        for h, k in zip(table.headers, table.keys, strict=False)
        if _PLATFORM_ID_COLUMN.match(k) and k not in columns
    )
    warnings = (
        [f"Spalten ohne Gegenstück in MusicHub ignoriert: {', '.join(ignored)}."] if ignored else []
    )
    return ParseResult("tunemymusic_csv", records, _group_playlists(placements), warnings)


# --- Soundiiz CSV --------------------------------------------------------------------
# Laut Soundiiz-Hilfe: Kopfzeile Pflicht, Spalte "title" Pflicht, dazu "artist", "album",
# "isrc"; Trennzeichen Komma oder Semikolon.

SOUNDIIZ_COLUMNS = {"title", "artist", "album", "isrc"}


def parse_soundiiz_csv(table: CsvTable, filename: str) -> ParseResult:
    columns: dict[str, FieldSetter] = {
        "title": _set("title"),
        "artist": _set("artist"),
        "album": _set("album"),
        "isrc": _isrc,
    }
    records, _ = _table_records(table, columns)
    return ParseResult("soundiiz_csv", records)


# --- Exportify CSV (Spotify) ---------------------------------------------------------
# Spalten laut Exportify-README, z. B. "Track URI", "Track Name", "Artist Name(s)",
# "Album Name", "Album Release Date", "Track Duration (ms)", "ISRC".


def parse_exportify_csv(table: CsvTable, filename: str) -> ParseResult:
    def track_uri(record: ImportRecord, value: str) -> None:
        if value.startswith("spotify:local:"):
            record.warnings.append("Lokale Spotify-Datei ohne Spotify-ID.")
        else:
            add_url(value, record, expected=Platform.SPOTIFY)

    columns: dict[str, FieldSetter] = {
        "track uri": track_uri,
        "track name": _set("title"),
        "artist name(s)": _set("artist"),
        "album name": _set("album"),
        "album release date": _release,
        "track duration (ms)": _duration("ms"),
        "isrc": _isrc,
    }
    records, _ = _table_records(table, columns)
    return ParseResult("exportify_csv", records)


# --- Allgemeines CSV -------------------------------------------------------------------


def parse_generic_csv(table: CsvTable, filename: str) -> ParseResult:
    records, placements = _table_records(table, GENERIC_COLUMNS, playlist_columns=PLAYLIST_COLUMNS)
    return ParseResult("generic_csv", records, _group_playlists(placements))


TITLE_ALIASES = {
    "title",
    "track",
    "track name",
    "track title",
    "song",
    "song name",
    "name",
    "titel",
    "songtitel",
}
ARTIST_ALIASES = {
    "artist",
    "artists",
    "artist name",
    "artist name(s)",
    "artist(s)",
    "interpret",
    "künstler",
}


def detect_csv(table: CsvTable) -> str:
    keys = {k for k in table.keys if k}
    if "musichub_id" in keys and {"title", "artist", "playlist"} <= keys:
        return "musichub_csv"
    if {"track uri", "track name", "artist name(s)"} <= keys:
        return "exportify_csv"
    if {"track name", "artist name"} <= keys and ("playlist name" in keys or "type" in keys):
        return "tunemymusic_csv"
    if "title" in keys and keys <= SOUNDIIZ_COLUMNS:
        return "soundiiz_csv"
    has_title = bool(keys & TITLE_ALIASES)
    has_artist = bool(keys & ARTIST_ALIASES)
    if has_title and has_artist:
        return "generic_csv"
    found = ", ".join(h for h in table.headers if h) or "keine"
    raise FormatError(
        "Spalten nicht erkannt. Benötigt werden mindestens Titel und Artist "
        f"(z. B. „title“ und „artist“). Gefunden: {found}."
    )


CSV_PARSERS = {
    "musichub_csv": parse_musichub_csv,
    "tunemymusic_csv": parse_tunemymusic_csv,
    "soundiiz_csv": parse_soundiiz_csv,
    "exportify_csv": parse_exportify_csv,
    "generic_csv": parse_generic_csv,
}


# --- JSON ------------------------------------------------------------------------------


def _record_from_object(obj: Any, row: int, columns: dict[str, FieldSetter]) -> ImportRecord:
    record = ImportRecord(row=row)
    if not isinstance(obj, dict):
        record.raw = {"wert": str(obj)[:500]}
        record.errors.append("Eintrag ist kein Objekt.")
        return record
    flat: dict[str, str] = {}
    for key, value in obj.items():
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            flat[normalize_header(str(key))] = str(value).strip()
        elif isinstance(value, list) and all(isinstance(v, str) for v in value):
            flat[normalize_header(str(key))] = "|".join(value)
    record.raw = {k: v for k, v in flat.items()}
    apply_columns(record, flat, columns)
    return record


def parse_musichub_json(data: dict[str, Any], filename: str, max_rows: int) -> ParseResult:
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        raise FormatError("MusicHub-JSON ohne „tracks“-Liste.")
    if len(tracks) > max_rows:
        raise FormatError(f"Die Datei hat mehr als {max_rows} Tracks.")
    records: list[ImportRecord] = []
    by_id: dict[str, int] = {}
    for i, t in enumerate(tracks, start=1):
        record = ImportRecord(row=i)
        if not isinstance(t, dict):
            record.errors.append("Track-Eintrag ist kein Objekt.")
            records.append(record)
            continue
        record.raw = {k: v for k, v in t.items() if k != "sources"}
        record.musichub_id = parse_uuid(t.get("id"))
        record.title = _str(t.get("title"))
        record.artist = _str(t.get("artist"))
        record.album = _str(t.get("album"))
        record.version = _str(t.get("version"))
        record.notes = _str(t.get("notes"))
        record.duration_ms = parse_duration(t.get("duration_ms"), record, unit="ms")
        record.isrc = parse_isrc(t.get("isrc"), record)
        record.release_date = parse_release_date(t.get("release_date"), record)
        record.tags = [str(x) for x in t.get("tags") or [] if isinstance(x, str)]
        for s in t.get("sources") or []:
            if not isinstance(s, dict) or s.get("link_status", "linked") != "linked":
                continue
            try:
                platform = Platform(str(s.get("platform")))
                external_id = validate_external_id(platform, str(s.get("external_id", "")))
            except (ValueError, PlatformUrlError):
                record.warnings.append(
                    f"Quelle {s.get('platform')}/{s.get('external_id')} ungültig, ignoriert."
                )
                continue
            record.sources.append(
                SourceRef(
                    platform.value,
                    external_id,
                    _str(s.get("url")) or canonical_url(platform, external_id),
                )
            )
        records.append(record.finalize())
        if record.musichub_id:
            by_id[record.musichub_id] = len(records) - 1

    playlists: list[ParsedPlaylist] = []
    for p in data.get("playlists") or []:
        if not isinstance(p, dict) or not _str(p.get("name")):
            continue
        entries = [by_id[str(tid)] for tid in p.get("tracks") or [] if str(tid) in by_id]
        playlists.append(
            ParsedPlaylist(
                name=_str(p.get("name")) or "",
                description=_str(p.get("description")),
                entries=entries,
            )
        )
    return ParseResult("musichub_json", records, playlists)


def parse_generic_json(data: Any, filename: str, max_rows: int) -> ParseResult:
    records: list[ImportRecord] = []
    placements: list[tuple[int, str, int | None]] = []
    descriptions: dict[str, str | None] = {}

    def add(items: list[Any], playlist: str | None) -> None:
        for position, obj in enumerate(items):
            if len(records) >= max_rows:
                raise FormatError(f"Die Datei hat mehr als {max_rows} Tracks.")
            record = _record_from_object(obj, len(records) + 1, GENERIC_COLUMNS).finalize()
            records.append(record)
            name = playlist or (
                first({k: v for k, v in record.raw.items()}, PLAYLIST_COLUMNS) if record.raw else ""
            )
            if name:
                placements.append((len(records) - 1, name, position))

    if isinstance(data, list):
        add(data, None)
    elif isinstance(data, dict) and isinstance(data.get("playlists"), list):
        for p in data["playlists"]:
            if isinstance(p, dict) and isinstance(p.get("tracks"), list):
                name = _str(p.get("name")) or _str(p.get("title")) or "Import"
                descriptions[name] = _str(p.get("description"))
                add(p["tracks"], name)
        if isinstance(data.get("tracks"), list):
            add(data["tracks"], None)
    elif isinstance(data, dict) and isinstance(data.get("tracks"), list):
        add(data["tracks"], _str(data.get("name")) or _str(data.get("playlist")))
    else:
        raise FormatError(
            "JSON-Aufbau nicht erkannt: erwartet wird eine Liste von Tracks, "
            "ein Objekt mit „tracks“ oder ein Objekt mit „playlists“."
        )
    return ParseResult("generic_json", records, _group_playlists(placements, descriptions))


def _str(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def parse_json(text: str, filename: str, max_rows: int, forced: str | None) -> ParseResult:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormatError(
            f"Ungültiges JSON (Zeile {exc.lineno}, Spalte {exc.colno}): {exc.msg}."
        ) from exc
    is_musichub = isinstance(data, dict) and data.get("format") == "musichub"
    if forced == "musichub_json" or (forced is None and is_musichub):
        if not isinstance(data, dict):
            raise FormatError("MusicHub-JSON muss ein Objekt sein.")
        if data.get("version") not in (1, None):
            raise FormatError(
                f"MusicHub-JSON-Version {data.get('version')} wird nicht unterstützt."
            )
        return parse_musichub_json(data, filename, max_rows)
    return parse_generic_json(data, filename, max_rows)


# --- TXT-Tracklisten -----------------------------------------------------------------
# Eine Zeile = ein Track im Muster "Artist - Titel". Optional: Nummerierung am Anfang
# ("1." / "1)"), Dauer am Ende ("3:45" oder "(3:45)"), Links irgendwo in der Zeile.
# Leere Zeilen und Zeilen, die mit "#" beginnen, werden übersprungen.

_TXT_LINK = re.compile(r"(https?://\S+|spotify:track:[A-Za-z0-9]+)")
_TXT_NUMBER = re.compile(r"^\s*\d{1,4}[.)]\s+")
_TXT_DURATION = re.compile(r"\s*[\(\[]?(\d{1,2}:\d{2}(?::\d{2})?)[\)\]]?\s*$")
_TXT_SEPARATOR = re.compile(r"\s+[-–—]\s+")


def parse_txt(text: str, filename: str, max_rows: int) -> ParseResult:
    records: list[ImportRecord] = []
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if len(records) >= max_rows:
            raise FormatError(f"Die Datei hat mehr als {max_rows} Zeilen mit Tracks.")
        record = ImportRecord(row=number, raw={"zeile": stripped[:1000]})
        for link in _TXT_LINK.findall(stripped):
            add_url(link.rstrip(".,;)"), record)
        rest = _TXT_LINK.sub(" ", stripped)
        rest = _TXT_NUMBER.sub("", rest)
        duration = _TXT_DURATION.search(rest)
        if duration:
            record.duration_ms = parse_duration(duration.group(1), record)
            rest = rest[: duration.start()]
        rest = rest.strip(" -–—|\t")
        parts = _TXT_SEPARATOR.split(rest, maxsplit=1)
        reason = None
        if len(parts) == 2:
            record.artist, record.title = parts[0].strip(), parts[1].strip()
        elif rest:
            record.title = rest
            reason = "Artist nicht erkennbar – erwartet wird „Artist - Titel“."
        else:
            reason = "Nur ein Link – ohne Titel und Artist kann kein Track angelegt werden."
        record.finalize()
        if reason:
            missing = {"Titel fehlt.", "Artist fehlt."}
            record.errors = [reason, *(e for e in record.errors if e not in missing)]
        records.append(record)
    return ParseResult("txt", records)


def default_playlist_name(filename: str) -> str:
    stem = PurePosixPath(filename).stem.strip()
    return re.sub(r"[_]+", " ", stem)[:300] or "Import"
