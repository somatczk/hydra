"""Paper account state, idempotent paper trade rows, per-session balance snapshots.

Revision ID: 0002_paper_state
Revises: 0001
Create Date: 2026-10-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_paper_state"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS paper_accounts (
        session_id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        quote_asset TEXT NOT NULL,
        state JSONB NOT NULL,
        state_version BIGINT NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    ALTER TABLE trades
        ADD COLUMN IF NOT EXISTS order_id TEXT,
        ADD COLUMN IF NOT EXISTS fill_seq BIGINT,
        ADD COLUMN IF NOT EXISTS net_pnl NUMERIC(24,8),
        ADD COLUMN IF NOT EXISTS liquidity TEXT,
        ADD COLUMN IF NOT EXISTS reason TEXT
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_trades_session_fill_seq"
    " ON trades(session_id, fill_seq) WHERE fill_seq IS NOT NULL",
    "ALTER TABLE balance_snapshots ADD COLUMN IF NOT EXISTS session_id TEXT",
    "CREATE INDEX IF NOT EXISTS ix_balance_snap_session_ts"
    " ON balance_snapshots(session_id, timestamp)",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("forward-only; restore from a pg_dump instead")
