"""Durable job queue for backtest, hyperopt and walk-forward runs.

Read by the engine, run by the worker.

Revision ID: 0006_backtest_jobs
Revises: 0005_backtest_metrics
Create Date: 2026-10-21

Additive and idempotent. ``kind`` has no CHECK so a new job kind needs no migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_backtest_jobs"
down_revision: str | None = "0005_backtest_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS backtest_jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
        request JSONB NOT NULL,
        strategy_config JSONB NOT NULL,
        progress DOUBLE PRECISION NOT NULL DEFAULT 0,
        detail JSONB NOT NULL DEFAULT '{}',
        result JSONB,
        result_id TEXT,
        error TEXT,
        cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
        attempts INTEGER NOT NULL DEFAULT 0,
        worker_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        started_at TIMESTAMPTZ,
        heartbeat_at TIMESTAMPTZ,
        finished_at TIMESTAMPTZ
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_backtest_jobs_queued ON backtest_jobs (created_at)"
    " WHERE status = 'queued'",
    "CREATE INDEX IF NOT EXISTS ix_backtest_jobs_kind ON backtest_jobs (kind, created_at DESC)",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_backtest_jobs_kind")
    op.execute("DROP INDEX IF EXISTS ix_backtest_jobs_queued")
    op.execute("DROP TABLE IF EXISTS backtest_jobs")
