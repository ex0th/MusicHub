"""CSV lesen: Trennzeichen erkennen, Kopfzeile normalisieren, Zeilennummern behalten."""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from musichub.domain.types import Platform
from musichub.transfer.records import (
    FormatError,
    ImportRecord,
    add_external_id,
    add_url,
    parse_duration,
    parse_isrc,
    parse_release_date,
    split_tags,
)

csv.field_size_limit(1_000_000)

DELIMITERS = [",", ";", "\t", "|"]


@dataclass(slots=True)
class CsvTable:
    headers: list[str]  # Originalschreibweise
    keys: list[str]  # normalisiert (klein, Leerraum/Striche vereinheitlicht)
    rows: list[tuple[int, dict[str, str]]]  # (Zeilennummer, normalisierter Schlüssel -> Wert)
    delimiter: str


def normalize_header(name: str) -> str:
    value = name.strip().strip('"').lower()
    value = re.sub(r"[–—]", "-", value)
    value = re.sub(r"\s*-\s*", " - ", value)
    return re.sub(r"\s+", " ", value)


def _detect_delimiter(header_line: str) -> str:
    counts = {d: header_line.count(d) for d in DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def read_csv(text: str, *, max_rows: int) -> CsvTable:
    lines = text.splitlines()
    first = next((line for line in lines if line.strip()), "")
    delimiter = _detect_delimiter(first)
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    try:
        headers: list[str] | None = None
        rows: list[tuple[int, dict[str, str]]] = []
        for values in reader:
            if not any(v.strip() for v in values):
                continue
            if headers is None:
                headers = [h.strip() for h in values]
                continue
            if len(rows) >= max_rows:
                raise FormatError(f"Die Datei hat mehr als {max_rows} Datenzeilen.")
            keys = [normalize_header(h) for h in headers]
            row: dict[str, str] = {}
            for i, key in enumerate(keys):
                if key and i < len(values):
                    row.setdefault(key, values[i].strip())
            if len(values) > len(keys) and any(v.strip() for v in values[len(keys) :]):
                row["__extra__"] = "1"
            rows.append((reader.line_num, row))
    except csv.Error as exc:
        raise FormatError(f"CSV-Datei ist beschädigt (Zeile {reader.line_num}): {exc}") from exc
    if headers is None:
        raise FormatError("Die CSV-Datei enthält keine Kopfzeile.")
    if len([h for h in headers if h]) < 1:
        raise FormatError("Die Kopfzeile der CSV-Datei ist leer.")
    return CsvTable(
        headers=headers,
        keys=[normalize_header(h) for h in headers],
        rows=rows,
        delimiter=delimiter,
    )


# --- Spaltenzuordnung per Alias ------------------------------------------------------

FieldSetter = Callable[[ImportRecord, str], None]


def _set(name: str) -> FieldSetter:
    def setter(record: ImportRecord, value: str) -> None:
        if value and not getattr(record, name):
            setattr(record, name, value)

    return setter


def _duration(unit: str) -> FieldSetter:
    def setter(record: ImportRecord, value: str) -> None:
        if record.duration_ms is None:
            record.duration_ms = parse_duration(value, record, unit=unit)

    return setter


def _isrc(record: ImportRecord, value: str) -> None:
    if record.isrc is None:
        record.isrc = parse_isrc(value, record)


def _release(record: ImportRecord, value: str) -> None:
    if record.release_date is None:
        record.release_date = parse_release_date(value, record)


def _url(platform: Platform | None) -> FieldSetter:
    def setter(record: ImportRecord, value: str) -> None:
        for part in re.split(r"[\s|]+", value):
            if platform is None or "://" in part or part.startswith("spotify:"):
                add_url(part, record, expected=platform)
            else:
                add_external_id(platform, part, record)

    return setter


def _tags(record: ImportRecord, value: str) -> None:
    record.tags.extend(split_tags(value))


def _genre(record: ImportRecord, value: str) -> None:
    record.tags.extend(f"Genre:{g}" for g in split_tags(value.replace(",", "|")))


#: Allgemeine Spaltennamen (normalisiert) -> Zielfeld. Reihenfolge = Priorität.
GENERIC_COLUMNS: dict[str, FieldSetter] = {
    **dict.fromkeys(
        [
            "title",
            "track",
            "track name",
            "track title",
            "song",
            "song name",
            "name",
            "titel",
            "songtitel",
        ],
        _set("title"),
    ),
    **dict.fromkeys(
        [
            "artist",
            "artists",
            "artist name",
            "artist name(s)",
            "artist(s)",
            "interpret",
            "künstler",
        ],
        _set("artist"),
    ),
    **dict.fromkeys(["album", "album name", "album title"], _set("album")),
    **dict.fromkeys(["version", "mix", "remix"], _set("version")),
    **dict.fromkeys(["duration", "length", "time", "dauer", "länge"], _duration("auto")),
    **dict.fromkeys(
        ["duration (ms)", "duration_ms", "track duration (ms)", "duration ms"], _duration("ms")
    ),
    **dict.fromkeys(["duration_s", "duration (s)", "seconds"], _duration("s")),
    "isrc": _isrc,
    **dict.fromkeys(
        ["release date", "release_date", "album release date", "released", "veröffentlicht"],
        _release,
    ),
    **dict.fromkeys(["url", "link", "uri", "track uri", "urls", "links"], _url(None)),
    **dict.fromkeys(["spotify url", "spotify_url", "spotify link"], _url(Platform.SPOTIFY)),
    **dict.fromkeys(["youtube url", "youtube_url", "youtube link"], _url(Platform.YOUTUBE)),
    **dict.fromkeys(
        ["soundcloud url", "soundcloud_url", "soundcloud link"], _url(Platform.SOUNDCLOUD)
    ),
    **dict.fromkeys(["spotify id", "spotify_id", "spotify - id"], _url(Platform.SPOTIFY)),
    **dict.fromkeys(["youtube id", "youtube_id", "youtube - id"], _url(Platform.YOUTUBE)),
    **dict.fromkeys(
        ["soundcloud id", "soundcloud_id", "soundcloud - id"], _url(Platform.SOUNDCLOUD)
    ),
    "tags": _tags,
    "genre": _genre,
    **dict.fromkeys(["notes", "notizen", "comment", "kommentar"], _set("notes")),
}

PLAYLIST_COLUMNS = ["playlist", "playlist name", "playlist_name"]
POSITION_COLUMNS = ["position", "playlist_position", "playlist position", "#", "nr", "nr."]


def apply_columns(
    record: ImportRecord, row: dict[str, str], columns: dict[str, FieldSetter]
) -> None:
    for key, setter in columns.items():
        value = row.get(key)
        if value:
            setter(record, value)


def raw_row(table: CsvTable, row: dict[str, str]) -> dict[str, Any]:
    return {h: row.get(k, "") for h, k in zip(table.headers, table.keys, strict=False) if h}


def first(row: dict[str, str], names: list[str]) -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return ""
