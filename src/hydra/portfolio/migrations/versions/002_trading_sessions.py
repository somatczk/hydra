"""Add trading_sessions, risk_config tables and source column to trades/positions.

Revision ID: 002_trading_sessions
Revises: 001_portfolio_schema
Create Date: 2026-03-14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic revision identifiers
revision = "002_trading_sessions"
down_revision = "001_portfolio_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- trading_sessions ----
    op.create_table(
        "trading_sessions",
        sa.Column("id", sa.Text, primary_key=True),
        sa.Column("strategy_id", sa.Text, nullable=False),
        sa.Column("trading_mode", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="stopped"),
        sa.Column("exchange_id", sa.Text, nullable=False, server_default="binance"),
        sa.Column("symbols", sa.ARRAY(sa.Text), nullable=False),
        sa.Column("timeframe", sa.Text, nullable=False),
        sa.Column("paper_capital", sa.Numeric(24, 8), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_trading_sessions_strategy", "trading_sessions", ["strategy_id"])
    op.create_index("ix_trading_sessions_status", "trading_sessions", ["status"])

    # ---- risk_config ----
    op.create_table(
        "risk_config",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("scope", sa.Text, nullable=False, unique=True),
        sa.Column("max_position_pct", sa.Numeric(8, 4), server_default="0.10"),
        sa.Column("max_risk_per_trade", sa.Numeric(8, 4), server_default="0.02"),
        sa.Column("max_daily_loss_pct", sa.Numeric(8, 4), server_default="0.03"),
        sa.Column("max_drawdown_pct", sa.Numeric(8, 4), server_default="0.15"),
        sa.Column("max_concurrent_positions", sa.Integer, server_default="10"),
        sa.Column("kill_switch_active", sa.Boolean, server_default="false"),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )
    # Seed global row
    op.execute("INSERT INTO risk_config (scope) VALUES ('global')")

    # ---- Add source column to existing tables ----
    op.add_column(
        "trades",
        sa.Column("source", sa.Text, server_default="live"),
    )
    op.add_column(
        "positions",
        sa.Column("source", sa.Text, server_default="live"),
    )


def downgrade() -> None:
    op.drop_column("positions", "source")
    op.drop_column("trades", "source")
    op.drop_table("risk_config")
    op.drop_table("trading_sessions")
