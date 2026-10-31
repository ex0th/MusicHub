"""Neutrales Zwischenformat aller Import-Adapter und gemeinsame Feldparser.

Jeder Adapter erzeugt ein ``ParseResult``: eine Liste von ``ImportRecord`` (eine Zeile
bzw. ein Track der Datei) und optional ``ParsedPlaylist``-Einträge, die auf Records
verweisen. Ein fehlerhafter Datensatz ist ein Record mit ``errors`` – er bricht den
Import nie ab, sondern erscheint im Bericht.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from musichub.domain.platform_urls import (
    PlatformUrlError,
    canonical_url,
    parse_url,
    validate_external_id,
)
from musichub.domain.types import Platform

MAX_TEXT = {"title": 500, "artist": 500, "album": 500, "version": 200, "notes": 10_000}
_ISRC = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


@dataclass(slots=True)
class SourceRef:
    platform: str
    external_id: str
    url: str


@dataclass(slots=True)
class ImportRecord:
    #: Zeilennummer in der Datei (1-basiert, Kopfzeile mitgezählt) bzw. Index im JSON
    row: int
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    version: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    release_date: str | None = None  # ISO-Datum
    notes: str | None = None
    #: "Kategorie:Name" oder "Name"
    tags: list[str] = field(default_factory=list)
    sources: list[SourceRef] = field(default_factory=list)
    #: Track-ID aus einem MusicHub-Export (für verlustfreien Wiederimport)
    musichub_id: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: Originalwerte der Zeile, für Vorschau und Bericht
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImportRecord:
        data = dict(data)
        data["sources"] = [SourceRef(**s) for s in data.get("sources", [])]
        return cls(**data)

    def finalize(self) -> ImportRecord:
        """Pflichtfelder und Längen prüfen; danach ist ``errors`` vollständig."""
        for name, limit in MAX_TEXT.items():
            value = getattr(self, name)
            if isinstance(value, str):
                value = " ".join(value.split()) if name != "notes" else value.strip()
                value = value or None
                if value and len(value) > limit:
                    self.errors.append(f"{name} ist länger als {limit} Zeichen.")
                setattr(self, name, value)
        if not self.title:
            self.errors.append("Titel fehlt.")
        if not self.artist:
            self.errors.append("Artist fehlt.")
        # Doppelte Quellen derselben Zeile entfernen
        seen: set[tuple[str, str]] = set()
        unique: list[SourceRef] = []
        for s in self.sources:
            if (s.platform, s.external_id) not in seen:
                seen.add((s.platform, s.external_id))
                unique.append(s)
        self.sources = unique
        return self


@dataclass(slots=True)
class ParsedPlaylist:
    name: str
    description: str | None = None
    #: Indizes in ``ParseResult.records`` in Playlist-Reihenfolge (Duplikate erlaubt)
    entries: list[int] = field(default_factory=list)


@dataclass(slots=True)
class ParseResult:
    format: str
    records: list[ImportRecord]
    playlists: list[ParsedPlaylist] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class FormatError(ValueError):
    """Datei passt nicht zum Format (z. B. Spalten nicht erkannt) – ganze Datei abgelehnt."""


# --- Feldparser ------------------------------------------------------------------
# Jeder Parser liefert None bei leerem Wert und meldet Probleme über ``record``.

_DURATION = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})(?:[.,]\d+)?$")


def parse_duration(value: Any, record: ImportRecord, *, unit: str = "auto") -> int | None:
    """``3:45``, ``1:02:03``, Sekunden (``225``) oder Millisekunden (``unit='ms'``)."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    match = _DURATION.match(text)
    if match:
        hours = int(match.group(1) or 0)
        ms = ((hours * 60 + int(match.group(2))) * 60 + int(match.group(3))) * 1000
    else:
        try:
            number = float(text.replace(",", "."))
        except ValueError:
            record.warnings.append(f"Dauer „{text}“ nicht lesbar, wird ignoriert.")
            return None
        if unit == "ms":
            ms = round(number)
        elif unit == "s":
            ms = round(number * 1000)
        else:  # Heuristik: über 10 000 sind es Millisekunden (≈ 2:46 h als Sekunden)
            ms = round(number) if number > 10_000 else round(number * 1000)
    if ms <= 0 or ms > 24 * 3600 * 1000:
        record.warnings.append(f"Dauer „{text}“ ist unplausibel, wird ignoriert.")
        return None
    return ms


def parse_release_date(value: Any, record: ImportRecord) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern, order in (
        (r"^(\d{4})-(\d{2})-(\d{2})", "ymd"),
        (r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$", "dmy"),
    ):
        m = re.match(pattern, text)
        if m:
            parts = [int(p) for p in m.groups()]
            y, mo, d = parts if order == "ymd" else (parts[2], parts[1], parts[0])
            try:
                return date(y, mo, d).isoformat()
            except ValueError:
                break
    if re.match(r"^\d{4}(-\d{2})?$", text):
        record.warnings.append(
            f"Nur Jahr/Monat bekannt („{text}“), Veröffentlichungsdatum bleibt leer."
        )
        return None
    record.warnings.append(f"Datum „{text}“ nicht lesbar, wird ignoriert.")
    return None


def parse_isrc(value: Any, record: ImportRecord) -> str | None:
    text = re.sub(r"[\s-]", "", str(value or "")).upper()
    if not text:
        return None
    if not _ISRC.match(text):
        record.warnings.append(f"ISRC „{value}“ ist ungültig und wird ignoriert.")
        return None
    return text


def add_url(value: Any, record: ImportRecord, *, expected: Platform | None = None) -> None:
    text = str(value or "").strip()
    if not text:
        return
    try:
        parsed = parse_url(text)
    except PlatformUrlError as exc:
        record.warnings.append(f"Link ignoriert: {exc}")
        return
    if expected and parsed.platform is not expected:
        record.warnings.append(f"Link „{text}“ gehört nicht zu {expected.value}, wird ignoriert.")
        return
    record.sources.append(SourceRef(parsed.platform.value, parsed.external_id, parsed.url))


def add_external_id(platform: Platform, value: Any, record: ImportRecord) -> None:
    text = str(value or "").strip()
    if not text:
        return
    if "/" in text and "://" in text:  # versehentlich ein Link in der ID-Spalte
        add_url(text, record, expected=platform)
        return
    try:
        external_id = validate_external_id(platform, text)
    except PlatformUrlError:
        record.warnings.append(
            f"{platform.value}-ID „{text}“ hat ein unbekanntes Format, wird ignoriert."
        )
        return
    record.sources.append(
        SourceRef(platform.value, external_id, canonical_url(platform, external_id))
    )


def parse_uuid(value: Any) -> str | None:
    try:
        return str(uuid.UUID(str(value).strip())) if value else None
    except ValueError:
        return None


def split_tags(value: Any) -> list[str]:
    text = str(value or "")
    return [t.strip() for t in re.split(r"[|;]", text) if t.strip()]


#: Prefix, mit dem der MusicHub-Export Zellen gegen Formula-Injection schützt
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def unescape_formula(value: str) -> str:
    """Kehrt den Export-Schutz um: "'=SUMME" -> "=SUMME" (nur MusicHub-Dateien)."""
    if len(value) > 1 and value[0] == "'" and value[1] in FORMULA_PREFIXES:
        return value[1:]
    return value
