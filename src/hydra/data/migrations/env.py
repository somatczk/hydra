"""Alembic environment: async engine on asyncpg, one transaction, serialised by an advisory lock."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from hydra.data.migrations.runner import MIGRATION_LOCK_ID, async_dsn, resolve_dsn


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=None)
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        context.run_migrations()


async def _run_online(dsn: str) -> None:
    engine = create_async_engine(async_dsn(dsn), poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


def _run_offline(dsn: str) -> None:
    context.configure(url=async_dsn(dsn), target_metadata=None, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline(resolve_dsn(context.config))
else:
    asyncio.run(_run_online(resolve_dsn(context.config)))
