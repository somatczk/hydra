"""Baseline: the 2.2.0 inline schema, made idempotent, plus system_config.

Revision ID: 0001
Revises:
Create Date: 2026-10-20

Every statement is IF NOT EXISTS, so it upgrades an empty database and a database created by the
2.1.x/2.2.0 inline DDL alike. No continuous aggregates, compression or retention policies.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESERIES: tuple[str, ...] = (
    "CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE",
    "CREATE SCHEMA IF NOT EXISTS ts",
    """
    CREATE TABLE IF NOT EXISTS ts.ohlcv_1m (
        exchange TEXT NOT NULL, symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL, timestamp TIMESTAMPTZ NOT NULL,
        open NUMERIC NOT NULL, high NUMERIC NOT NULL,
        low NUMERIC NOT NULL, close NUMERIC NOT NULL,
        volume NUMERIC NOT NULL,
        UNIQUE (exchange, symbol, timeframe, timestamp)
    )
    """,
    "SELECT create_hypertable('ts.ohlcv_1m', 'timestamp', if_not_exists => TRUE,"
    " migrate_data => TRUE)",
    """
    CREATE TABLE IF NOT EXISTS ts.trades (
        exchange TEXT NOT NULL, symbol TEXT NOT NULL,
        trade_id TEXT NOT NULL, price NUMERIC NOT NULL,
        quantity NUMERIC NOT NULL, side TEXT NOT NULL,
        timestamp TIMESTAMPTZ NOT NULL
    )
    """,
    "SELECT create_hypertable('ts.trades', 'timestamp', if_not_exists => TRUE,"
    " migrate_data => TRUE)",
    """
    CREATE TABLE IF NOT EXISTS ts.funding_rates (
        exchange TEXT NOT NULL, symbol TEXT NOT NULL,
        rate NUMERIC NOT NULL, next_funding_time TIMESTAMPTZ,
        timestamp TIMESTAMPTZ NOT NULL
    )
    """,
    "SELECT create_hypertable('ts.funding_rates', 'timestamp', if_not_exists => TRUE,"
    " migrate_data => TRUE)",
)

_RELATIONAL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS trading_sessions (
        id TEXT PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        trading_mode TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'stopped',
        exchange_id TEXT NOT NULL DEFAULT 'binance',
        symbols TEXT[] NOT NULL DEFAULT ARRAY['BTCUSDT'],
        timeframe TEXT NOT NULL DEFAULT '1h',
        paper_capital NUMERIC(24,8),
        started_at TIMESTAMPTZ,
        stopped_at TIMESTAMPTZ,
        error_message TEXT,
        positions_at_stop INTEGER NOT NULL DEFAULT 0,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE trading_sessions"
    " ADD COLUMN IF NOT EXISTS positions_at_stop INTEGER NOT NULL DEFAULT 0",
    """
    CREATE TABLE IF NOT EXISTS risk_config (
        id SERIAL PRIMARY KEY,
        scope TEXT NOT NULL UNIQUE,
        max_position_pct NUMERIC(8,4) DEFAULT 0.10,
        max_risk_per_trade NUMERIC(8,4) DEFAULT 0.02,
        max_portfolio_heat NUMERIC(8,4) DEFAULT 0.06,
        max_daily_loss_pct NUMERIC(8,4) DEFAULT 0.03,
        max_drawdown_pct NUMERIC(8,4) DEFAULT 0.15,
        max_consecutive_losses INTEGER DEFAULT 5,
        max_concurrent_positions INTEGER DEFAULT 10,
        kill_switch_active BOOLEAN DEFAULT FALSE,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "ALTER TABLE risk_config ADD COLUMN IF NOT EXISTS max_portfolio_heat NUMERIC(8,4) DEFAULT 0.06",
    "ALTER TABLE risk_config ADD COLUMN IF NOT EXISTS max_consecutive_losses INTEGER DEFAULT 5",
    "INSERT INTO risk_config (scope) VALUES ('global') ON CONFLICT (scope) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS trades (
        id SERIAL PRIMARY KEY,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        quantity NUMERIC(24,8) NOT NULL,
        price NUMERIC(24,8) NOT NULL,
        fee NUMERIC(24,8) NOT NULL DEFAULT 0,
        pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
        strategy_id TEXT NOT NULL,
        exchange_id TEXT NOT NULL DEFAULT 'binance',
        source TEXT NOT NULL DEFAULT 'live',
        session_id TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_trades_strategy ON trades(strategy_id)",
    "CREATE INDEX IF NOT EXISTS ix_trades_ts ON trades(timestamp)",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS notes TEXT DEFAULT ''",
    "ALTER TABLE trades ADD COLUMN IF NOT EXISTS tags TEXT[] DEFAULT '{}'",
    """
    CREATE TABLE IF NOT EXISTS positions (
        id SERIAL PRIMARY KEY,
        strategy_id TEXT NOT NULL,
        session_id TEXT,
        exchange_id TEXT NOT NULL DEFAULT 'binance',
        symbol TEXT NOT NULL,
        direction TEXT NOT NULL,
        quantity NUMERIC(24,8) NOT NULL DEFAULT 0,
        avg_entry_price NUMERIC(24,8) NOT NULL DEFAULT 0,
        unrealized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        realized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'live',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        UNIQUE (session_id, symbol)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS balance_snapshots (
        id SERIAL PRIMARY KEY,
        timestamp TIMESTAMPTZ NOT NULL DEFAULT now(),
        total_value NUMERIC(24,8) NOT NULL,
        unrealized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        realized_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        drawdown_pct NUMERIC(8,4) NOT NULL DEFAULT 0,
        peak_value NUMERIC(24,8) NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'live'
    )
    """,
    "ALTER TABLE balance_snapshots ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live'",
    "CREATE INDEX IF NOT EXISTS ix_balance_snap_ts ON balance_snapshots(timestamp)",
    """
    CREATE TABLE IF NOT EXISTS exchange_credentials (
        exchange_id TEXT PRIMARY KEY,
        encrypted_key TEXT NOT NULL,
        encrypted_secret TEXT NOT NULL,
        encrypted_passphrase TEXT,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_preferences (
        id SERIAL PRIMARY KEY,
        preferences JSONB NOT NULL DEFAULT '{}',
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_state (
        id SERIAL PRIMARY KEY,
        scope TEXT NOT NULL UNIQUE DEFAULT 'global',
        daily_pnl NUMERIC(24,8) NOT NULL DEFAULT 0,
        daily_pnl_date DATE NOT NULL DEFAULT CURRENT_DATE,
        consecutive_losses INTEGER NOT NULL DEFAULT 0,
        last_loss_at TIMESTAMPTZ,
        current_drawdown NUMERIC(8,4) NOT NULL DEFAULT 0,
        peak_portfolio_value NUMERIC(24,8) NOT NULL DEFAULT 0,
        updated_at TIMESTAMPTZ DEFAULT now()
    )
    """,
    "INSERT INTO risk_state (scope) VALUES ('global') ON CONFLICT (scope) DO NOTHING",
    """
    CREATE TABLE IF NOT EXISTS backtest_results (
        id TEXT PRIMARY KEY,
        strategy TEXT NOT NULL,
        period TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'completed',
        total_trades INTEGER NOT NULL DEFAULT 0,
        win_rate DOUBLE PRECISION NOT NULL DEFAULT 0,
        total_pnl DOUBLE PRECISION NOT NULL DEFAULT 0,
        max_drawdown DOUBLE PRECISION NOT NULL DEFAULT 0,
        sharpe_ratio DOUBLE PRECISION NOT NULL DEFAULT 0,
        equity_curve JSONB NOT NULL DEFAULT '[]',
        name TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS name TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE backtest_results"
    " ADD COLUMN IF NOT EXISTS transactions JSONB NOT NULL DEFAULT '[]'",
    """
    CREATE TABLE IF NOT EXISTS backtest_result_trades (
        id SERIAL PRIMARY KEY,
        backtest_id TEXT NOT NULL REFERENCES backtest_results(id)
            ON DELETE CASCADE,
        entry_time TIMESTAMPTZ NOT NULL,
        exit_time TIMESTAMPTZ NOT NULL,
        side TEXT NOT NULL,
        entry_price DOUBLE PRECISION NOT NULL,
        exit_price DOUBLE PRECISION NOT NULL,
        pnl DOUBLE PRECISION NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_backtest_result_trades_backtest"
    " ON backtest_result_trades(backtest_id)",
    """
    ALTER TABLE backtest_results
        ADD COLUMN IF NOT EXISTS strategy_id TEXT,
        ADD COLUMN IF NOT EXISTS initial_capital DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS data_source TEXT,
        ADD COLUMN IF NOT EXISTS data_exchange TEXT,
        ADD COLUMN IF NOT EXISTS data_symbol TEXT,
        ADD COLUMN IF NOT EXISTS data_timeframe TEXT,
        ADD COLUMN IF NOT EXISTS bar_count INTEGER,
        ADD COLUMN IF NOT EXISTS first_ts TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS last_ts TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS gap_count INTEGER,
        ADD COLUMN IF NOT EXISTS requested_start TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS requested_end TIMESTAMPTZ,
        ADD COLUMN IF NOT EXISTS stopped_reason TEXT,
        ADD COLUMN IF NOT EXISTS benchmark_equity JSONB NOT NULL DEFAULT '[]',
        ADD COLUMN IF NOT EXISTS benchmark_return DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS alpha DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS beta DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS max_consecutive_wins INTEGER,
        ADD COLUMN IF NOT EXISTS max_consecutive_losses INTEGER,
        ADD COLUMN IF NOT EXISTS expectancy DOUBLE PRECISION
    """,
    # Rows without provenance ran on synthetic bars; trades go with them via the FK.
    "DELETE FROM backtest_results WHERE data_source IS NULL",
    "ALTER TABLE backtest_results ALTER COLUMN data_source SET NOT NULL",
)

_SYSTEM_CONFIG: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS system_config (
        key TEXT PRIMARY KEY,
        value JSONB NOT NULL,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_by TEXT
    )
    """,
    """
    INSERT INTO system_config (key, value) VALUES
        ('trading_mode', '"paper"'),
        ('default_pair', '"BTC/USDT"'),
        ('default_timeframe', '"1h"'),
        ('max_concurrent_strategies', '5'),
        ('paper_capital', '10000')
    ON CONFLICT (key) DO NOTHING
    """,
)


def upgrade() -> None:
    for statement in (*_TIMESERIES, *_RELATIONAL, *_SYSTEM_CONFIG):
        op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("the baseline has no downgrade; restore from a pg_dump instead")
