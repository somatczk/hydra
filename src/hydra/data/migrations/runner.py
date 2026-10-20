"""Programmatic Alembic: the engine, the CLI and tests run migrations through these functions.

The script location is this package, so the image needs no alembic.ini. Upgrades take a
transaction-scoped advisory lock (see env.py), so concurrent starts serialise.
"""

from __future__ import annotations

import os
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

MIGRATION_LOCK_ID = 0x4859_4452
DSN_ENV = "DATABASE_URL"
_SCRIPT_LOCATION = str(Path(__file__).resolve().parent)


class MigrationError(RuntimeError):
    """The schema cannot be brought to the single Alembic head."""


def async_dsn(dsn: str) -> str:
    """``postgresql://`` -> ``postgresql+asyncpg://`` for SQLAlchemy."""
    for prefix in ("postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn.removeprefix(prefix)
    if dsn.startswith("postgresql+asyncpg://"):
        return dsn
    raise MigrationError("database DSN must start with postgresql://")


def alembic_config(dsn: str | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", _SCRIPT_LOCATION)
    if dsn is not None:
        cfg.attributes["dsn"] = dsn
    return cfg


def resolve_dsn(cfg: Config) -> str:
    """The DSN handed over by the runner, else DATABASE_URL; there is no default."""
    dsn = cfg.attributes.get("dsn") or os.environ.get(DSN_ENV)
    if not dsn:
        raise MigrationError(f"{DSN_ENV} is not set; refusing to guess a database")
    return str(dsn)


def heads() -> list[str]:
    return list(ScriptDirectory.from_config(alembic_config()).get_heads())


def head_revision() -> str:
    """The single head revision; more than one head is a broken chain."""
    found = heads()
    if len(found) != 1:
        raise MigrationError(f"expected exactly one Alembic head, found {sorted(found)}")
    return found[0]


def upgrade_to_head(dsn: str) -> None:
    """Blocking upgrade; call it from a worker thread inside an event loop."""
    head_revision()
    command.upgrade(alembic_config(dsn), "head")


async def current_revision(dsn: str) -> str | None:
    """The revision stored in the database, or None when it was never migrated."""
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        exists = await conn.fetchval("SELECT to_regclass('public.alembic_version') IS NOT NULL")
        if not exists:
            return None
        rows = await conn.fetch("SELECT version_num FROM alembic_version")
    finally:
        await conn.close()
    if len(rows) > 1:
        raise MigrationError(f"alembic_version has {len(rows)} rows")
    return str(rows[0]["version_num"]) if rows else None
