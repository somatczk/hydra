"""Initial TimescaleDB schema for market data.

Revision ID: 001
Create Date: 2025-01-01 00:00:00.000000

Creates the ``ts`` schema with hypertables for OHLCV bars, trades, and
funding rates.  Adds continuous aggregates for higher timeframes,
compression after 7 days, and retention after 2 years.
"""

from __future__ import annotations

from alembic import op

# Alembic revision identifiers
revision = "001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the ts schema, hypertables, aggregates, and policies."""

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    op.execute("CREATE SCHEMA IF NOT EXISTS ts")

    # ------------------------------------------------------------------
    # Enable TimescaleDB
    # ------------------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE")

    # ------------------------------------------------------------------
    # ts.ohlcv_1m — 1-minute OHLCV bars
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS ts.ohlcv_1m (
            exchange    TEXT        NOT NULL,
            symbol      TEXT        NOT NULL,
            timeframe   TEXT        NOT NULL,
            timestamp   TIMESTAMPTZ NOT NULL,
            open        NUMERIC     NOT NULL,
            high        NUMERIC     NOT NULL,
            low         NUMERIC     NOT NULL,
            close       NUMERIC     NOT NULL,
            volume      NUMERIC     NOT NULL,
            UNIQUE (exchange, symbol, timeframe, timestamp)
        )
    """)

    op.execute("SELECT create_hypertable('ts.ohlcv_1m', 'timestamp', if_not_exists => TRUE)")

    # ------------------------------------------------------------------
    # ts.trades
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS ts.trades (
            exchange    TEXT        NOT NULL,
            symbol      TEXT        NOT NULL,
            trade_id    TEXT        NOT NULL,
            price       NUMERIC     NOT NULL,
            quantity    NUMERIC     NOT NULL,
            side        TEXT        NOT NULL,
            timestamp   TIMESTAMPTZ NOT NULL
        )
    """)

    op.execute("SELECT create_hypertable('ts.trades', 'timestamp', if_not_exists => TRUE)")

    # ------------------------------------------------------------------
    # ts.funding_rates
    # ------------------------------------------------------------------
    op.execute("""
        CREATE TABLE IF NOT EXISTS ts.funding_rates (
            exchange            TEXT        NOT NULL,
            symbol              TEXT        NOT NULL,
            rate                NUMERIC     NOT NULL,
            next_funding_time   TIMESTAMPTZ,
            timestamp           TIMESTAMPTZ NOT NULL
        )
    """)

    op.execute("SELECT create_hypertable('ts.funding_rates', 'timestamp', if_not_exists => TRUE)")

    # ------------------------------------------------------------------
    # Continuous aggregates — higher timeframes from 1m data
    # ------------------------------------------------------------------
    _aggregates = [
        ("5m", "5 minutes"),
        ("15m", "15 minutes"),
        ("1h", "1 hour"),
        ("4h", "4 hours"),
        ("1d", "1 day"),
        ("1w", "1 week"),
    ]

    for suffix, bucket in _aggregates:
        view_name = f"ts.ohlcv_{suffix}"
        cagg_tmpl = (
            "CREATE MATERIALIZED VIEW IF NOT EXISTS {vn}"
            " WITH (timescaledb.continuous) AS"
            " SELECT"
            "   exchange,"
            "   symbol,"
            "   time_bucket(INTERVAL '{bk}', timestamp) AS timestamp,"
            "   first(open, timestamp)  AS open,"
            "   max(high)               AS high,"
            "   min(low)                AS low,"
            "   last(close, timestamp)  AS close,"
            "   sum(volume)             AS volume"
            " FROM ts.ohlcv_1m"
            " WHERE timeframe = '1m'"
            " GROUP BY exchange, symbol, time_bucket(INTERVAL '{bk}', timestamp)"
            " WITH NO DATA"
        )
        op.execute(cagg_tmpl.format(vn=view_name, bk=bucket))

        # Refresh policy: keep aggregates up to date
        op.execute(f"""
            SELECT add_continuous_aggregate_policy('{view_name}',
                start_offset    => INTERVAL '3 days',
                end_offset      => INTERVAL '1 minute',
                schedule_interval => INTERVAL '{bucket}',
                if_not_exists   => TRUE
            )
        """)

    # ------------------------------------------------------------------
    # Compression policy — compress chunks older than 7 days
    # ------------------------------------------------------------------
    op.execute("""
        ALTER TABLE ts.ohlcv_1m SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'exchange, symbol, timeframe'
        )
    """)
    op.execute("""
        SELECT add_compression_policy('ts.ohlcv_1m',
            compress_after => INTERVAL '7 days',
            if_not_exists  => TRUE
        )
    """)

    op.execute("""
        ALTER TABLE ts.trades SET (
            timescaledb.compress,
            timescaledb.compress_segmentby = 'exchange, symbol'
        )
    """)
    op.execute("""
        SELECT add_compression_policy('ts.trades',
            compress_after => INTERVAL '7 days',
            if_not_exists  => TRUE
        )
    """)

    # ------------------------------------------------------------------
    # Retention policy — drop data older than 2 years
    # ------------------------------------------------------------------
    op.execute("""
        SELECT add_retention_policy('ts.ohlcv_1m',
            drop_after     => INTERVAL '2 years',
            if_not_exists  => TRUE
        )
    """)
    op.execute("""
        SELECT add_retention_policy('ts.trades',
            drop_after     => INTERVAL '2 years',
            if_not_exists  => TRUE
        )
    """)
    op.execute("""
        SELECT add_retention_policy('ts.funding_rates',
            drop_after     => INTERVAL '2 years',
            if_not_exists  => TRUE
        )
    """)


def downgrade() -> None:
    """Drop everything in reverse order."""
    # Drop continuous aggregates
    for suffix in ("1w", "1d", "4h", "1h", "15m", "5m"):
        op.execute(f"DROP MATERIALIZED VIEW IF EXISTS ts.ohlcv_{suffix} CASCADE")

    # Drop hypertables
    op.execute("DROP TABLE IF EXISTS ts.funding_rates CASCADE")
    op.execute("DROP TABLE IF EXISTS ts.trades CASCADE")
    op.execute("DROP TABLE IF EXISTS ts.ohlcv_1m CASCADE")

    # Drop schema
    op.execute("DROP SCHEMA IF EXISTS ts CASCADE")
