"""Vergleicht den Migrationsstand der Datenbank mit dem Code (für /api/health/ready)."""

from __future__ import annotations

from functools import lru_cache
from importlib.resources import files

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import ProgrammingError


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(files("musichub") / "migrations"))
    if database_url:
        cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return cfg


@lru_cache
def code_heads() -> frozenset[str]:
    return frozenset(ScriptDirectory.from_config(alembic_config()).get_heads())


def database_revisions(conn: Connection) -> frozenset[str]:
    try:
        rows = conn.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    except ProgrammingError:
        conn.rollback()
        return frozenset()
    return frozenset(rows)
