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


def sql_in(enum: type[StrEnum]) -> str:
    """Hilfsfunktion für CHECK-Constraints: "'a', 'b', 'c'"."""
    return ", ".join(f"'{m.value}'" for m in enum)
