from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Principal:
    """Der authentifizierte Aufrufer, unabhängig vom Verfahren."""

    subject: str
    display_name: str
    groups: frozenset[str] = field(default_factory=frozenset)
    can_write: bool = False
    auth_method: str = "unknown"
