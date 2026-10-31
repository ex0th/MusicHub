"""Hintergrund-Worker: ``python -m musichub.jobs.worker`` bzw. ``musichub-worker``.

Läuft als eigener Container. Holt Jobs aus PostgreSQL, führt sie aus und beendet sich
bei SIGTERM sauber nach dem aktuellen Job (wichtig für Arcane-Updates).
"""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import uuid

from musichub.connectors.base import ConnectorError
from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import get_settings
from musichub.core.logging import configure_logging, correlation_id
from musichub.db.migrate import schema_is_current, wait_for_database
from musichub.db.session import get_sessionmaker
from musichub.jobs import queue
from musichub.jobs.handlers import HANDLERS, JobContext, JobError
from musichub.models import Job

log = logging.getLogger("musichub.worker")


def run_once(worker_id: str, connectors: ConnectorRegistry) -> bool:
    """Bearbeitet höchstens einen Job. Gibt True zurück, wenn einer bearbeitet wurde."""
    sessions = get_sessionmaker()

    with sessions.begin() as session:
        job = queue.claim_next(session, worker_id)
        if job is None:
            return False
        job_id, kind, payload = job.id, job.kind, dict(job.payload)

    def progress(current: int, total: int | None, message: str | None) -> None:
        try:
            with sessions.begin() as s:
                queue.set_progress(s, job_id, current, total, message)
        except Exception:  # Fortschritt ist Komfort – nie den Job daran scheitern lassen
            log.warning("Fortschritt konnte nicht gespeichert werden", exc_info=True)

    ctx_result: dict[str, object] = {}

    token = correlation_id.set(f"job-{job_id.hex[:12]}")
    try:
        log.info("Starte Job %s", kind)
        fn = HANDLERS.get(kind)
        try:
            if fn is None:
                raise JobError(f"Unbekannter Job-Typ '{kind}'", retryable=False)
            with sessions.begin() as session:
                fn(
                    JobContext(
                        session=session,
                        connectors=connectors,
                        payload=payload,
                        job_id=job_id,
                        progress=progress,
                        result=ctx_result,
                    )
                )
        except (JobError, ConnectorError) as exc:
            _fail(job_id, str(exc), retryable=exc.retryable, retry_after=exc.retry_after)
        except Exception as exc:
            log.exception("Job %s abgebrochen", kind)
            _fail(job_id, f"{type(exc).__name__}: {exc}", retryable=True)
        else:
            with sessions.begin() as session:
                queue.mark_succeeded(session, job_id, dict(ctx_result))
            log.info("Job %s erfolgreich", kind)
    finally:
        correlation_id.reset(token)
    return True


def _fail(
    job_id: uuid.UUID, error: str, *, retryable: bool, retry_after: float | None = None
) -> None:
    with get_sessionmaker().begin() as session:
        job = session.get(Job, job_id)
        if job is not None:
            queue.mark_failed(session, job, error, retryable=retryable, retry_after=retry_after)
            log.warning(
                "Job fehlgeschlagen (Versuch %d/%d): %s", job.attempts, job.max_attempts, error
            )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    connectors = ConnectorRegistry(default_connectors(settings))

    stop = threading.Event()

    def _shutdown(signum: int, _frame: object) -> None:
        log.info("Signal %s empfangen, beende nach aktuellem Job …", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    # Erst arbeiten, wenn das Schema aktuell ist (die API migriert beim Start).
    wait_for_database(settings.database_url, timeout_seconds=300)
    while not stop.is_set() and not schema_is_current(settings.database_url):
        log.info("Warte auf aktuelles Datenbankschema (Migration durch API) …")
        stop.wait(5)

    log.info("Worker %s gestartet (Job-Typen: %s)", worker_id, ", ".join(sorted(HANDLERS)))
    while not stop.is_set():
        try:
            worked = run_once(worker_id, connectors)
        except Exception:
            # z. B. Datenbank kurz weg – nicht abstürzen, sondern warten und erneut versuchen
            log.exception("Fehler in der Worker-Schleife")
            worked = False
        if not worked:
            stop.wait(settings.worker_poll_interval_seconds)
    log.info("Worker beendet")


if __name__ == "__main__":
    main()
