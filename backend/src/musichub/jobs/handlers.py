"""Registrierte Job-Typen. Neue Jobs: Funktion schreiben und mit ``@handler("art")`` anmelden.

Ein Handler bekommt eine eigene DB-Session und den JobContext. Er wirft
``JobError(retryable=False)`` für endgültige Fehler; ``ConnectorError`` übernimmt
die ``retryable``-Einstufung des Connectors; alles andere wird wiederholt.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from musichub.connectors.registry import ConnectorRegistry

log = logging.getLogger(__name__)


class JobError(Exception):
    def __init__(
        self, message: str, *, retryable: bool = True, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after = retry_after


def _no_progress(current: int, total: int | None, message: str | None) -> None:
    return None


@dataclass(frozen=True, slots=True)
class JobContext:
    session: Session
    connectors: ConnectorRegistry
    payload: dict[str, Any]
    job_id: uuid.UUID | None = None
    #: Fortschritt melden – wird sofort in eigener Transaktion gespeichert
    progress: Callable[[int, int | None, str | None], None] = _no_progress
    #: Ergebnis (Zähler, Hinweise), wird bei Erfolg am Job gespeichert
    result: dict[str, Any] = field(default_factory=dict)


Handler = Callable[[JobContext], None]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        if kind in HANDLERS:
            raise RuntimeError(f"Job-Typ '{kind}' doppelt registriert")
        HANDLERS[kind] = fn
        return fn

    return register


@handler("system.ping")
def ping(ctx: JobContext) -> None:
    """Diagnose-Job: prüft, dass Worker und Queue funktionieren."""
    log.info("Pong", extra={"payload": ctx.payload})


@handler("matching.scan")
def matching_scan(ctx: JobContext) -> None:
    """Matching-Vorschläge aktualisieren (z. B. nach einem Import). Idempotent."""
    from musichub.library.matching import scan

    result = scan(ctx.session)
    log.info("Matching-Vorschläge aktualisiert", extra={"new_candidates": result.created})


# Job-Typen der Plattform-Konten registrieren (Import am Ende vermeidet Zirkelbezüge)
from musichub.connections import importer as _platform_jobs  # noqa: E402, F401
from musichub.sync import executor as _sync_jobs  # noqa: E402, F401
