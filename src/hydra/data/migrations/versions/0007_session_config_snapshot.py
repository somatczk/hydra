"""Store the strategy config a session started with, so recovery resumes it.

Revision ID: 0007_session_config_snapshot
Revises: 0006_backtest_jobs
Create Date: 2026-10-21

Additive and idempotent. Rows from before this revision have NULL columns and recover from the
strategy YAML file.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_session_config_snapshot"
down_revision: str | None = "0006_backtest_jobs"
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
    for statement in (
        "ALTER TABLE trading_sessions DROP COLUMN IF EXISTS config_hash",
        "ALTER TABLE trading_sessions DROP COLUMN IF EXISTS strategy_config",
    ):
        op.execute(statement)
