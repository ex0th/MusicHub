"""Fachliche Aufzählungstypen, die DB, API, Connectoren und Engines gemeinsam nutzen.

In der Datenbank werden sie als Text mit CHECK-Constraint gespeichert, nicht als
PostgreSQL-ENUM: Neue Plattformen oder Status brauchen so nur eine kleine Migration.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    SPOTIFY = "spotify"
    SOUNDCLOUD = "soundcloud"
    YOUTUBE = "youtube"


class LinkStatus(StrEnum):
    """Wie sicher gehört eine Quelle zum lokalen Track?"""

    #: Bestätigte Zuordnung lokaler Track <-> Plattform-Track.
    LINKED = "linked"
    #: Vorschlag der Matching-Engine, wartet auf Bestätigung.
    CANDIDATE = "candidate"
    #: Vom Nutzer abgelehnter Vorschlag; wird nicht erneut vorgeschlagen.
    REJECTED = "rejected"


class Availability(StrEnum):
    """Ist der Plattform-Track (noch) abrufbar? Unabhängig von ``LinkStatus``."""

    UNKNOWN = "unknown"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class MatchMethod(StrEnum):
    MANUAL = "manual"
    ISRC = "isrc"
    FUZZY = "fuzzy"
    IMPORT = "import"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # wird erneut versucht
    DEAD = "dead"  # max_attempts erreicht, kein weiterer Versuch


class ImportStatus(StrEnum):
    #: Datei gelesen und abgeglichen, wartet auf Bestätigung
    ANALYZED = "analyzed"
    COMMITTED = "committed"
    DISCARDED = "discarded"
    #: Datei abgelehnt (Typ, Größe, Inhalt) – nur zur Protokollierung
    FAILED = "failed"


class ImportItemStatus(StrEnum):
    NEW = "new"
    EXISTING = "existing"
    POTENTIAL_DUPLICATE = "potential_duplicate"
    #: derselbe Track kommt in der Datei schon früher vor
    FILE_DUPLICATE = "file_duplicate"
    INVALID = "invalid"


class ImportDecision(StrEnum):
    CREATE = "create"
    USE_EXISTING = "use_existing"
    SKIP = "skip"


class ImportResult(StrEnum):
    CREATED = "created"
    LINKED = "linked"
    SKIPPED = "skipped"
    FAILED = "failed"


class PlaylistImportMode(StrEnum):
    #: neue Playlist anlegen
    CREATE = "create"
    #: in vorhandene Playlist gleichen Namens einfügen, was noch fehlt
    MERGE = "merge"
    #: Inhalt der vorhandenen Playlist durch die Datei ersetzen
    REPLACE = "replace"
    SKIP = "skip"


class MatchLevel(StrEnum):
    """Einstufung zweier lokaler Tracks durch die Matching-Engine."""

    #: sicher identisch – nur diese Stufe darf (ohne Blocker) automatisch zusammengeführt werden
    CERTAIN = "certain"
    #: wahrscheinlich identisch – Bestätigung durch den Nutzer nötig
    LIKELY = "likely"
    #: manuelle Prüfung notwendig
    REVIEW = "review"
    #: unterschiedliche Tracks – wird nicht vorgeschlagen
    DIFFERENT = "different"


class CandidateStatus(StrEnum):
    OPEN = "open"
    #: „Später prüfen“
    DEFERRED = "deferred"
    #: abgelehnt – wird bei neuen Scans nicht erneut vorgeschlagen
    REJECTED = "rejected"


class MergeMethod(StrEnum):
    MANUAL = "manual"
    AUTO = "auto"


class MergeStatus(StrEnum):
    MERGED = "merged"
    UNDONE = "undone"


class AccountStatus(StrEnum):
    """Zustand eines verbundenen Plattform-Kontos."""

    CONNECTED = "connected"
    #: Token abgelaufen/widerrufen – neu verbinden
    REAUTH_REQUIRED = "reauth_required"


class RemoteImportStatus(StrEnum):
    """Ergebnis des letzten Imports einer Plattform-Playlist."""

    OK = "ok"
    #: seit dem letzten Import unverändert (inkrementell übersprungen)
    UNCHANGED = "unchanged"
    ERROR = "error"


class SyncMode(StrEnum):
    #: nur fehlende Tracks anhängen – nie entfernen oder umsortieren
    ADD_ONLY = "add_only"
    #: Zielplaylist vollständig an die lokale angleichen (nur von MusicHub verwaltete,
    #: ausdrücklich freigegebene Ziele)
    MIRROR = "mirror"


class SyncTrigger(StrEnum):
    PREVIEW = "preview"
    MANUAL = "manual"
    SCHEDULED = "scheduled"


class SyncRunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    #: Vorschau (Dry-Run) fertig – nichts auf der Plattform geändert
    PREVIEW = "preview"
    SUCCEEDED = "succeeded"
    #: nichts zu tun
    NO_CHANGES = "no_changes"
    #: ein Teil ausgeführt (z. B. Kontingent erschöpft), nächster Lauf setzt fort
    PARTIAL = "partial"
    #: externe Änderungen brauchen Zustimmung – nichts geändert
    CONFLICT = "conflict"
    #: Sicherheitsregel verhindert den Lauf (Schreiben aus, keine Bestätigung …)
    BLOCKED = "blocked"
    FAILED = "failed"


class LookupStatus(StrEnum):
    """Ergebnis der Suche eines lokalen Tracks auf einer Plattform (ohne Verknüpfung)."""

    #: sicher gefunden (ISRC oder exakte Daten) – wird beim Sync verknüpft
    FOUND = "found"
    #: nur ähnlicher Treffer – Vorschlag, wird nicht automatisch verwendet
    CANDIDATE = "candidate"
    NOT_FOUND = "not_found"
    #: Suche fehlgeschlagen (API-Fehler, Kontingent) – wird erneut versucht
    API_ERROR = "api_error"


def sql_in(enum: type[StrEnum]) -> str:
    """Hilfsfunktion für CHECK-Constraints: "'a', 'b', 'c'"."""
    return ", ".join(f"'{m.value}'" for m in enum)
