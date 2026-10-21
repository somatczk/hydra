"""Store the strategy config a session started with, so recovery resumes it.

Revision ID: 0006_session_config_snapshot
Revises: 0005_backtest_metrics
Create Date: 2026-10-21

Additive and idempotent. Rows from before this revision have NULL columns and recover from the
strategy YAML file.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_session_config_snapshot"
down_revision: str | None = "0005_backtest_metrics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE trading_sessions ADD COLUMN IF NOT EXISTS strategy_config JSONB",
    "ALTER TABLE trading_sessions ADD COLUMN IF NOT EXISTS config_hash TEXT",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from a pg_dump instead")
