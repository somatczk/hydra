"""Risk state: account ledger and breaker columns on risk_state, the risk_events audit log.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-20

Additive and idempotent. Seeds the 'paper' risk_state row the RiskService uses; its
paper_capital is the capital the ledger was built on, so a change made while the engine is down
goes through the audited paper reset on the next start. The heat limit
becomes a cross-session gross-exposure cap, so the untouched 0.06 default moves to 0.30; a value
someone changed is kept.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    """
    ALTER TABLE risk_state
        ADD COLUMN IF NOT EXISTS equity NUMERIC(24,8),
        ADD COLUMN IF NOT EXISTS equity_at TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS day_start_equity NUMERIC(24,8),
        ADD COLUMN IF NOT EXISTS carried_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS paper_capital NUMERIC(24,8),
        ADD COLUMN IF NOT EXISTS cb_tier SMALLINT NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS cb_tier_since TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS cb_requires_review BOOLEAN NOT NULL DEFAULT FALSE,
        ADD COLUMN IF NOT EXISTS daily_halt_until TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS consecutive_pause_until TIMESTAMPTZ
    """,
    "ALTER TABLE risk_state ALTER COLUMN current_drawdown TYPE NUMERIC(12,8)",
    "INSERT INTO risk_state (scope) VALUES ('paper') ON CONFLICT (scope) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS risk_events (
        id BIGSERIAL PRIMARY KEY,
        ts TIMESTAMPTZ NOT NULL DEFAULT now(),
        scope TEXT NOT NULL,
        kind TEXT NOT NULL,
        tier SMALLINT,
        actor TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        details JSONB NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_risk_events_scope_ts ON risk_events(scope, ts DESC)",
    "ALTER TABLE risk_config ALTER COLUMN max_portfolio_heat SET DEFAULT 0.30",
    "UPDATE risk_config SET max_portfolio_heat = 0.30 WHERE max_portfolio_heat = 0.06",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("no downgrade; restore from a pg_dump instead")
