"""Live order ledger: live_orders + live_fills, and the 'live' risk_state row.

Revision ID: 0009_live_orders
Revises: 0008_session_created_by
Create Date: 2026-10-27

Additive and idempotent. ``live_orders`` is the live state machine: the PENDING_SUBMIT intent
is written before the exchange call (outbox-first), so a crash mid-submit never loses the
possibility of an order resting on the exchange. ``order_list_id`` is the OCO discriminator
(an OCO row's ``exchange_order_id`` is the orderListId) and ``leg_client_order_ids`` carries
the wire ``aboveClientOrderId``/``belowClientOrderId`` the poller resolves the legs by.
``live_fills`` is the claim row of the claim-apply-confirm pipeline (F2): UNIQUE
(exchange_id, symbol, trade_id) makes replaying the same Binance trade a no-op at the
database level and ``applied`` marks the ledger apply, so a transient failure is retried by
every later sweep instead of orphaning a real fill. The 'live' risk_state seed mirrors
0003's 'paper' row for the live-scoped RiskService (Q1); its capital baseline is
system_config 'live_capital'.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_live_orders"
down_revision: str | None = "0008_session_created_by"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS live_orders (
        id BIGSERIAL PRIMARY KEY,
        client_order_id TEXT NOT NULL UNIQUE,
        exchange_order_id TEXT,
        order_list_id TEXT,
        session_id TEXT NOT NULL,
        strategy_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        order_type TEXT NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        price NUMERIC(24,8),
        stop_price NUMERIC(24,8),
        filled_quantity NUMERIC(24,8),
        average_fill_price NUMERIC(24,8),
        fee NUMERIC(24,8),
        fee_currency TEXT,
        reduce_only BOOLEAN NOT NULL DEFAULT FALSE,
        tag TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        actor TEXT NOT NULL DEFAULT '',
        leg_client_order_ids TEXT[],
        submitted_at TIMESTAMPTZ,
        terminal_at TIMESTAMPTZ,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        error TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_live_orders_session_status ON live_orders(session_id, status)",
    "CREATE INDEX IF NOT EXISTS ix_live_orders_exchange_order"
    " ON live_orders(exchange_order_id) WHERE exchange_order_id IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS ix_live_orders_order_list"
    " ON live_orders(order_list_id) WHERE order_list_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS live_fills (
        id BIGSERIAL PRIMARY KEY,
        trade_id TEXT NOT NULL,
        client_order_id TEXT NOT NULL,
        session_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        price NUMERIC(24,8) NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        fee NUMERIC(24,8) NOT NULL,
        fee_currency TEXT,
        liquidity TEXT,
        timestamp TIMESTAMPTZ NOT NULL,
        applied BOOLEAN NOT NULL DEFAULT FALSE,
        UNIQUE (exchange_id, symbol, trade_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_live_fills_session_symbol"
    " ON live_fills(session_id, symbol, timestamp)",
    "CREATE INDEX IF NOT EXISTS ix_live_fills_unapplied"
    " ON live_fills(session_id) WHERE NOT applied",
    "INSERT INTO risk_state (scope) VALUES ('live') ON CONFLICT (scope) DO NOTHING",
)


def upgrade() -> None:
    for statement in _STATEMENTS:
        op.execute(statement)


def downgrade() -> None:
    for statement in (
        "DROP TABLE IF EXISTS live_fills",
        "DROP TABLE IF EXISTS live_orders",
        "DELETE FROM risk_state WHERE scope = 'live'",
    ):
        op.execute(statement)
