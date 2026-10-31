"""Alembic-Umgebung. Liest die Datenbank-URL aus den MusicHub-Settings."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from musichub.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Explizit gesetzte URL (Tests) hat Vorrang, sonst Umgebungsvariable.
# Bewusst nicht get_settings(): Migrationen sollen ohne Auth-Konfiguration laufen.
if not config.get_main_option("sqlalchemy.url"):
    url = os.environ.get(
        "MUSICHUB_DATABASE_URL", "postgresql+psycopg://musichub:musichub@localhost:5432/musichub"
    )
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # musichub.db.migrate reicht eine Verbindung herein, die den Advisory-Lock hält.
    existing = config.attributes.get("connection")
    if existing is not None:
        context.configure(connection=existing, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return

    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
