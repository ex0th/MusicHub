"""Datenbank-Engine und Sessions (SQLAlchemy 2, synchron, Treiber psycopg 3).

Synchron ist bewusst gewählt: FastAPI führt sync-Endpunkte im Threadpool aus, der
Worker bleibt einfach, und es gibt keine async/sync-Doppelwelt in der Domänenlogik.
"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from musichub.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(
        settings.database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=False)


def get_db() -> Iterator[Session]:
    """FastAPI-Dependency: eine Session pro Request, Commit erfolgt explizit im Service."""
    session = get_sessionmaker()()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
