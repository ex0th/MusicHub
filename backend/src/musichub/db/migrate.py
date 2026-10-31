"""Migrationen beim Start ausführen – sicher bei mehreren gleichzeitig startenden Containern.

Ein PostgreSQL-Advisory-Lock sorgt dafür, dass immer nur ein Prozess migriert; die
anderen warten und finden danach ein aktuelles Schema vor. Das ist wichtig für
Arcane-Updates, bei denen Services ohne feste Reihenfolge neu erstellt werden.
"""

from __future__ import annotations

import logging
import time

from alembic import command
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from musichub.core.config import get_settings
from musichub.core.logging import configure_logging
from musichub.db.migrations_state import alembic_config, code_heads, database_revisions

log = logging.getLogger("musichub.migrate")

#: Beliebige, aber feste Zahl für pg_advisory_lock ("MUSICHUB" als Ziffern).
LOCK_KEY = 6874_2443_8216


def wait_for_database(url: str, timeout_seconds: float = 60.0) -> None:
    engine = create_engine(url, pool_pre_ping=True)
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            try:
                with engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                return
            except OperationalError:
                if time.monotonic() > deadline:
                    raise
                log.info("Warte auf Datenbank …")
                time.sleep(2)
    finally:
        engine.dispose()


def upgrade_to_head(url: str) -> None:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": LOCK_KEY})
            conn.commit()
            try:
                before = database_revisions(conn)
                if before == code_heads():
                    log.info("Datenbankschema ist aktuell (%s)", ", ".join(sorted(before)))
                    return
                log.info(
                    "Migriere Datenbank von %s auf %s",
                    sorted(before) or "leer",
                    sorted(code_heads()),
                )
                cfg = alembic_config(url)
                cfg.attributes["connection"] = conn
                command.upgrade(cfg, "head")
                conn.commit()
                log.info("Migration abgeschlossen")
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
                conn.commit()
    finally:
        engine.dispose()


def schema_is_current(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return database_revisions(conn) == code_heads()
    finally:
        engine.dispose()


def main() -> None:
    """CLI ``musichub-migrate``: wartet auf die DB und migriert auf den neuesten Stand."""
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    wait_for_database(settings.database_url)
    upgrade_to_head(settings.database_url)


if __name__ == "__main__":
    main()
