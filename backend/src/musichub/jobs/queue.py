"""Operationen auf der Job-Tabelle. Mehrere Worker können parallel laufen."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from musichub.domain.types import JobStatus
from musichub.models import Job

#: Läuft ein Job länger als das, gilt der Worker als abgestürzt und der Job wird neu vergeben.
STALE_AFTER = timedelta(minutes=15)


def _now() -> datetime:
    return datetime.now(UTC)


def enqueue(
    session: Session,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    max_attempts: int = 5,
    run_after: datetime | None = None,
) -> Job:
    job = Job(
        kind=kind,
        payload=payload or {},
        status=JobStatus.QUEUED.value,
        attempts=0,
        max_attempts=max_attempts,
        run_after=run_after or _now(),
    )
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session, worker_id: str) -> Job | None:
    """Holt atomar den nächsten fälligen Job. Muss in eigener Transaktion laufen."""
    now = _now()
    stmt = (
        select(Job)
        .where(
            or_(
                Job.status.in_([JobStatus.QUEUED.value, JobStatus.FAILED.value])
                & (Job.run_after <= now),
                (Job.status == JobStatus.RUNNING.value) & (Job.locked_at < now - STALE_AFTER),
            )
        )
        .order_by(Job.run_after)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = session.scalars(stmt).first()
    if job is None:
        return None
    job.status = JobStatus.RUNNING.value
    job.attempts += 1
    job.locked_at = now
    job.locked_by = worker_id
    session.flush()
    return job


def mark_succeeded(
    session: Session, job_id: uuid.UUID, result: dict[str, Any] | None = None
) -> None:
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status=JobStatus.SUCCEEDED.value,
            finished_at=_now(),
            locked_at=None,
            last_error=None,
            result=result or None,
        )
    )


def set_progress(
    session: Session, job_id: uuid.UUID, current: int, total: int | None, message: str | None
) -> None:
    session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(progress={"current": current, "total": total, "message": message})
    )


#: Längste Wartezeit, die ein Rate-Limit erzwingen darf (YouTube: bis Mitternacht PT)
MAX_RETRY_AFTER = 26 * 3600


def mark_failed(
    session: Session,
    job: Job,
    error: str,
    *,
    retryable: bool,
    retry_after: float | None = None,
) -> None:
    """Exponentielles Backoff (30 s, 1 min, 2 min … max. 1 h) oder endgültig ``dead``.

    ``retry_after`` (z. B. aus ``Retry-After`` einer Plattform) ersetzt das Backoff."""
    if retryable and job.attempts < job.max_attempts:
        delay = min(30 * 2 ** (job.attempts - 1), 3600)
        if retry_after is not None:
            delay = min(max(retry_after, 1), MAX_RETRY_AFTER)
        values: dict[str, Any] = {
            "status": JobStatus.FAILED.value,
            "run_after": _now() + timedelta(seconds=delay),
        }
    else:
        values = {"status": JobStatus.DEAD.value, "finished_at": _now()}
    session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(**values, locked_at=None, locked_by=None, last_error=error[:4000])
    )
