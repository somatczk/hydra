"""Create portfolio tables: positions, trades, balance_snapshots, daily_pnl.

Revision ID: 001_portfolio_schema
Revises:
Create Date: 2026-03-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic revision identifiers
revision = "001_portfolio_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- positions ----
    op.create_table(
        "positions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("avg_entry_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("strategy_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("exchange_id", sa.String(16), nullable=False),
        sa.Column("market_type", sa.String(16), nullable=False, server_default="SPOT"),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_positions_symbol_exchange", "positions", ["symbol", "exchange_id"])
    op.create_index("ix_positions_strategy", "positions", ["strategy_id"])

    # ---- trades ----
    op.create_table(
        "trades",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("direction", sa.String(8), nullable=False),
        sa.Column("entry_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("exit_price", sa.Numeric(24, 8), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("fees", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("funding_cost", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("strategy_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("exchange_id", sa.String(16), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_trades_symbol_exchange", "trades", ["symbol", "exchange_id"])
    op.create_index("ix_trades_strategy", "trades", ["strategy_id"])
    op.create_index("ix_trades_exit_time", "trades", ["exit_time"])

    # ---- balance_snapshots ----
    op.create_table(
        "balance_snapshots",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("exchange_id", sa.String(16), nullable=False),
        sa.Column("asset", sa.String(16), nullable=False),
        sa.Column("free", sa.Numeric(24, 8), nullable=False),
        sa.Column("locked", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("total", sa.Numeric(24, 8), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_balance_snapshots_exchange_asset",
        "balance_snapshots",
        ["exchange_id", "asset"],
    )
    op.create_index("ix_balance_snapshots_timestamp", "balance_snapshots", ["timestamp"])

    # ---- daily_pnl ----
    op.create_table(
        "daily_pnl",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("date", sa.DateTime(timezone=True), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("total_pnl", sa.Numeric(24, 8), nullable=False),
        sa.Column("fees", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("funding", sa.Numeric(24, 8), nullable=False, server_default="0"),
        sa.Column("strategy_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("exchange_id", sa.String(16), nullable=False),
    )
    op.create_index("ix_daily_pnl_date_exchange", "daily_pnl", ["date", "exchange_id"])
    op.create_index("ix_daily_pnl_strategy", "daily_pnl", ["strategy_id"])


def downgrade() -> None:
    op.drop_table("daily_pnl")
    op.drop_table("balance_snapshots")
    op.drop_table("trades")
    op.drop_table("positions")
