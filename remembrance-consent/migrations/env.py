"""Alembic environment. PostgreSQL only: the append-only and role-grant
guarantees the migrations install have no SQLite equivalent."""
from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

import app.models  # noqa: F401  (populate metadata for autogenerate)
from app.db import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = (
        config.get_main_option("sqlalchemy.url")
        or os.environ.get("REMEMBRANCE_MIGRATION_DATABASE_URL")
        or os.environ.get("REMEMBRANCE_DATABASE_URL")
    )
    if not url:
        raise RuntimeError("set REMEMBRANCE_MIGRATION_DATABASE_URL to the owner-role PostgreSQL URL")
    return url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
