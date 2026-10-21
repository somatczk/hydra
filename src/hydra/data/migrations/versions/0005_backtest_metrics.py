"""Persist the remaining backtest metrics and purge results computed with the old formulas.

Revision ID: 0005_backtest_metrics
Revises: 0004_credentials_validated_at
Create Date: 2026-10-21

Additive and idempotent. Rows stored before this revision use 252-day annualisation and the old
fill timing, so they are not comparable and are deleted (their trades cascade). Every row written
after it has ``total_return`` set, so a re-run deletes nothing.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_backtest_metrics"
down_revision: str | None = "0004_credentials_validated_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS total_return DOUBLE PRECISION",
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS annualized_return DOUBLE PRECISION",
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS sortino_ratio DOUBLE PRECISION",
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS calmar_ratio DOUBLE PRECISION",
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS profit_factor DOUBLE PRECISION",
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS max_drawdown_duration_s BIGINT",
    "DELETE FROM backtest_results WHERE total_return IS NULL",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from a pg_dump instead")
