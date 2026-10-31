"""Hochgeladene Dateien prüfen und in Text umwandeln.

Prüfungen (in dieser Reihenfolge):
1. Dateiname: nur ``.csv``, ``.tsv``, ``.txt``, ``.json``; Pfadanteile werden entfernt.
2. Größe: harte Obergrenze, unabhängig vom angegebenen Content-Length.
3. Inhalt: keine Binärdaten (NUL-Bytes, Steuerzeichen), dekodierbar als UTF-8 (mit oder
   ohne BOM) oder UTF-16 mit BOM. Windows-1252 (typischer Excel-Export) wird als
   Notlösung akzeptiert und im Bericht als Warnung vermerkt.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath

ALLOWED_EXTENSIONS = {".csv": "csv", ".tsv": "csv", ".txt": "txt", ".json": "json"}

# Erlaubt: Tab, LF, CR. Andere C0-Steuerzeichen deuten auf Binärdaten hin.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class UploadError(ValueError):
    """Datei wird abgelehnt; die Meldung geht so an den Nutzer."""


class UploadTooLargeError(UploadError):
    pass


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return (
            f"{n / (1024 * 1024):.0f} MB"
            if n % (1024 * 1024) == 0
            else f"{n / (1024 * 1024):.1f} MB"
        )
    return f"{max(1, n // 1024)} KB"


@dataclass(frozen=True, slots=True)
class DecodedFile:
    filename: str
    kind: str  # csv | txt | json
    text: str
    size: int
    sha256: str
    encoding: str
    warnings: tuple[str, ...] = field(default_factory=tuple)


def safe_filename(raw: str | None) -> str:
    name = PureWindowsPath(PurePosixPath(raw or "").name).name.strip()
    name = re.sub(r"[^\w.\- ()äöüÄÖÜß]", "_", name)[:200]
    return name or "upload"


def decode_upload(raw_name: str | None, data: bytes, *, max_bytes: int) -> DecodedFile:
    filename = safe_filename(raw_name)
    suffix = PurePosixPath(filename.lower()).suffix
    kind = ALLOWED_EXTENSIONS.get(suffix)
    if kind is None:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UploadError(
            f"Dateityp „{suffix or 'ohne Endung'}“ wird nicht unterstützt ({allowed})."
        )
    if len(data) == 0:
        raise UploadError("Die Datei ist leer.")
    if len(data) > max_bytes:
        raise UploadTooLargeError(f"Die Datei ist größer als {human_size(max_bytes)}.")

    warnings: list[str] = []
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text, encoding = data.decode("utf-16"), "utf-16"
    else:
        if b"\x00" in data:
            raise UploadError("Die Datei enthält Binärdaten und ist keine Textdatei.")
        try:
            text, encoding = data.decode("utf-8-sig"), "utf-8"
        except UnicodeDecodeError:
            try:
                text, encoding = data.decode("cp1252"), "windows-1252"
            except UnicodeDecodeError as exc:
                raise UploadError(
                    "Die Kodierung der Datei ist nicht lesbar (erwartet UTF-8)."
                ) from exc
            warnings.append(
                "Datei ist nicht UTF-8-kodiert, sie wurde als Windows-1252 gelesen. "
                "Sonderzeichen bitte in der Vorschau prüfen."
            )
    text = text.lstrip("\ufeff")
    if _CONTROL.search(text):
        raise UploadError("Die Datei enthält Steuerzeichen und ist keine gültige Textdatei.")
    if not text.strip():
        raise UploadError("Die Datei enthält keine Daten.")

    return DecodedFile(
        filename=filename,
        kind=kind,
        text=text,
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        encoding=encoding,
        warnings=tuple(warnings),
    )
