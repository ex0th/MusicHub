from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from musichub.connectors.platforms import default_connectors
from musichub.connectors.registry import ConnectorRegistry
from musichub.core.config import get_settings
from musichub.db.session import get_sessionmaker
from musichub.domain.types import JobStatus
from musichub.jobs import queue
from musichub.jobs.handlers import HANDLERS, JobContext, JobError
from musichub.jobs.worker import run_once
from musichub.models import Job

pytestmark = pytest.mark.db


@pytest.fixture
def registry() -> ConnectorRegistry:
    return ConnectorRegistry(default_connectors(get_settings()))


@pytest.fixture
def flaky_handler():  # type: ignore[no-untyped-def]
    calls: list[dict] = []

    def handler(ctx: JobContext) -> None:
        calls.append(ctx.payload)
        if ctx.payload.get("fail") == "retry":
            raise RuntimeError("temporär")
        if ctx.payload.get("fail") == "final":
            raise JobError("endgültig", retryable=False)

    HANDLERS["test.flaky"] = handler
    yield calls
    del HANDLERS["test.flaky"]


def _enqueue(kind: str, payload: dict | None = None, **kw) -> Job:  # type: ignore[no-untyped-def]
    with get_sessionmaker().begin() as s:
        job = queue.enqueue(s, kind, payload, **kw)
        s.expunge(job)
    return job


def _reload(db: Session, job: Job) -> Job:
    db.expire_all()
    fresh = db.get(Job, job.id)
    assert fresh is not None
    return fresh


def test_ping_job_succeeds(db: Session, registry: ConnectorRegistry) -> None:
    job = _enqueue("system.ping", {"hello": "world"})
    assert run_once("w1", registry) is True
    fresh = _reload(db, job)
    assert fresh.status == JobStatus.SUCCEEDED
    assert fresh.attempts == 1
    assert run_once("w1", registry) is False  # Queue leer


def test_retryable_failure_is_rescheduled(
    db: Session, registry: ConnectorRegistry, flaky_handler
) -> None:  # type: ignore[no-untyped-def]
    job = _enqueue("test.flaky", {"fail": "retry"})
    run_once("w1", registry)
    fresh = _reload(db, job)
    assert fresh.status == JobStatus.FAILED
    assert fresh.run_after > datetime.now(UTC)
    assert "temporär" in (fresh.last_error or "")
    assert run_once("w1", registry) is False  # noch nicht wieder fällig


def test_final_failure_and_max_attempts(
    db: Session, registry: ConnectorRegistry, flaky_handler
) -> None:  # type: ignore[no-untyped-def]
    final = _enqueue("test.flaky", {"fail": "final"})
    run_once("w1", registry)
    assert _reload(db, final).status == JobStatus.DEAD

    exhausted = _enqueue("test.flaky", {"fail": "retry"}, max_attempts=1)
    run_once("w1", registry)
    assert _reload(db, exhausted).status == JobStatus.DEAD


def test_unknown_job_kind_is_dead(db: Session, registry: ConnectorRegistry) -> None:
    job = _enqueue("does.not.exist")
    run_once("w1", registry)
    fresh = _reload(db, job)
    assert fresh.status == JobStatus.DEAD
    assert "Unbekannter Job-Typ" in (fresh.last_error or "")


def test_connector_error_from_job_is_not_retried(db: Session, registry: ConnectorRegistry) -> None:
    from musichub.domain.types import Platform

    def search(ctx: JobContext) -> None:
        ctx.connectors.call(
            Platform.SPOTIFY,
            lambda c: c.authorization_url(state="s", code_challenge="c", redirect_uri="r"),
        )

    HANDLERS["test.search"] = search
    try:
        job = _enqueue("test.search")
        run_once("w1", registry)
        fresh = _reload(db, job)
        assert fresh.status == JobStatus.DEAD  # nicht konfiguriert -> nicht wiederholbar
        assert "nicht konfiguriert" in (fresh.last_error or "")
    finally:
        del HANDLERS["test.search"]


def test_skip_locked_prevents_double_claim(db: Session) -> None:
    _enqueue("system.ping")
    sessions = get_sessionmaker()
    with sessions.begin() as s1, sessions.begin() as s2:
        first = queue.claim_next(s1, "w1")
        second = queue.claim_next(s2, "w2")
        assert first is not None
        assert second is None


def test_migrate_is_idempotent_with_lock(db: Session) -> None:
    from musichub.core.config import get_settings
    from musichub.db.migrate import schema_is_current, upgrade_to_head

    url = get_settings().database_url
    assert schema_is_current(url)
    upgrade_to_head(url)  # bereits aktuell -> No-op, Lock wird wieder freigegeben
    upgrade_to_head(url)
    assert schema_is_current(url)
