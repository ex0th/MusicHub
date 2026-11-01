"""Gemeinsame Fixtures.

DB-Tests laufen gegen eine echte PostgreSQL-Datenbank (TEST_DATABASE_URL). Das Schema
wird einmal pro Testlauf per ``alembic upgrade head`` angelegt – damit sind auch die
Migrationen getestet. Zwischen den Tests werden alle Tabellen geleert.
Ohne TEST_DATABASE_URL werden DB-Tests übersprungen, reine Logiktests laufen trotzdem.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

TEST_DB_URL = os.environ.get("TEST_DATABASE_URL")
DEV_TOKEN = "test-token-0123456789abcdef"

# Muss vor dem ersten Import von musichub.* gesetzt sein (Settings werden gecacht).
os.environ.update(
    {
        "MUSICHUB_APP_ENV": "test",
        "MUSICHUB_AUTH_MODE": "dev",
        "MUSICHUB_DEV_AUTH_TOKEN": DEV_TOKEN,
        "MUSICHUB_LOG_LEVEL": "WARNING",
        "MUSICHUB_DATABASE_URL": TEST_DB_URL
        or "postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid",
    }
)
for key in [
    k
    for k in os.environ
    if k.startswith(
        ("MUSICHUB_SPOTIFY", "MUSICHUB_SOUNDCLOUD", "MUSICHUB_YOUTUBE", "MUSICHUB_OIDC")
    )
]:
    del os.environ[key]

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from musichub.core.config import get_settings  # noqa: E402
from musichub.db.session import get_engine, get_sessionmaker  # noqa: E402


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if TEST_DB_URL:
        return
    skip = pytest.mark.skip(reason="TEST_DATABASE_URL nicht gesetzt")
    for item in items:
        if "db" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def migrated_db() -> Iterator[None]:
    from alembic import command

    from musichub.db.migrations_state import alembic_config

    assert TEST_DB_URL
    cfg = alembic_config(TEST_DB_URL)
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture
def db(migrated_db: None) -> Iterator[Session]:
    with get_engine().begin() as conn:
        conn.execute(
            text(
                "TRUNCATE sync_runs, platform_lookups, connector_accounts, oauth_states, remote_playlists, match_candidates, track_merges, import_items, import_runs, playlist_items, playlist_targets, track_sources, "
                "track_tags, tags, "
                "playlists, tracks, jobs"
            )
        )
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(scope="session")
def app():  # type: ignore[no-untyped-def]
    from musichub.main import create_app

    return create_app(get_settings())


@pytest.fixture
def client(app) -> Iterator[TestClient]:  # type: ignore[no-untyped-def]
    with TestClient(app) as c:
        yield c


@pytest.fixture
def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {DEV_TOKEN}"}
