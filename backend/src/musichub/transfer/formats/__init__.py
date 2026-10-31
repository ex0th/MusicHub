"""Formaterkennung und Einstieg für alle Import-Adapter."""

from __future__ import annotations

from musichub.transfer.decoding import DecodedFile
from musichub.transfer.formats.adapters import (
    CSV_PARSERS,
    FORMATS,
    FormatInfo,
    default_playlist_name,
    detect_csv,
    parse_json,
    parse_txt,
)
from musichub.transfer.formats.csv_common import read_csv
from musichub.transfer.records import FormatError, ParseResult

__all__ = ["FORMATS", "FormatInfo", "default_playlist_name", "detect_and_parse"]


def detect_and_parse(
    decoded: DecodedFile, *, max_rows: int, forced_format: str | None = None
) -> ParseResult:
    """Format erkennen (oder erzwungenes verwenden) und Datei einlesen.

    Wirft ``FormatError``, wenn die Datei als Ganzes nicht lesbar ist. Fehler in
    einzelnen Datensätzen stehen dagegen in den Records.
    """
    if forced_format is not None:
        info = FORMATS.get(forced_format)
        if info is None:
            raise FormatError(f"Unbekanntes Format „{forced_format}“.")
        if info.kind != decoded.kind:
            raise FormatError(
                f"{info.label} erwartet eine .{info.kind}-Datei, hochgeladen wurde .{decoded.kind}."
            )

    if decoded.kind == "json":
        result = parse_json(decoded.text, decoded.filename, max_rows, forced_format)
    elif decoded.kind == "txt" and forced_format in (None, "txt"):
        result = parse_txt(decoded.text, decoded.filename, max_rows)
    else:
        table = read_csv(decoded.text, max_rows=max_rows)
        fmt = forced_format or detect_csv(table)
        result = CSV_PARSERS[fmt](table, decoded.filename)
        if table.delimiter != ",":
            names = {";": "Semikolon", "\t": "Tabulator", "|": "senkrechter Strich"}
            result.warnings.append(f"Trennzeichen erkannt: {names[table.delimiter]}.")

    if not result.records:
        raise FormatError("Die Datei enthält keine Datensätze.")
    result.warnings[:0] = list(decoded.warnings)
    return result
