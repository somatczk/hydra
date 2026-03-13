"""Alembic environment configuration for Hydra TimescaleDB migrations.

Supports both online (live connection) and offline (SQL generation) modes.
Database DSN is read from ``hydra.core.config``.
"""

from __future__ import annotations

import os

from alembic import context

# ---------------------------------------------------------------------------
# Target metadata — we use raw SQL migrations so no SQLAlchemy metadata.
# ---------------------------------------------------------------------------
target_metadata = None


def _get_dsn() -> str:
    """Resolve the database DSN from environment or Hydra config."""
    dsn = os.environ.get("HYDRA_DATABASE_DSN")
    if dsn:
        return dsn

    # Fall back to Hydra config
    try:
        from hydra.core.config import load_config

        cfg = load_config()
        return (
            f"postgresql://{cfg.database.user}:{cfg.database.password}"
            f"@{cfg.database.host}:{cfg.database.port}/{cfg.database.name}"
        )
    except Exception:
        return "postgresql://hydra:hydra@localhost:5432/hydra"


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode — emit SQL to stdout."""
    url = _get_dsn()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    from sqlalchemy import create_engine

    connectable = create_engine(_get_dsn())

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
