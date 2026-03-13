"""Seed database with sample data for development.

Creates sample strategies, positions, trades, and balance snapshots
so the dashboard can be developed without connecting to a real exchange.

Usage::

    python -m scripts.seed_db --dsn postgresql://hydra:hydra_dev@localhost:5432/hydra

"""
# ruff: noqa: S311 -- random is fine for non-cryptographic seed data

from __future__ import annotations

import argparse
import asyncio
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed the Hydra database with sample development data.",
    )
    parser.add_argument(
        "--dsn",
        type=str,
        default="postgresql://hydra:hydra_dev@localhost:5432/hydra",
        help="TimescaleDB connection DSN",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=90,
        help="Number of historical days to seed (default: 90)",
    )
    parser.add_argument(
        "--trades-per-day",
        type=int,
        default=5,
        help="Average number of trades per day (default: 5)",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop and recreate seed tables before inserting",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without modifying the database",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Sample data generators
# ---------------------------------------------------------------------------

STRATEGIES = [
    {"id": "strat-lstm-momentum", "name": "LSTM Momentum", "exchange": "binance"},
    {"id": "strat-mean-reversion", "name": "Mean Reversion", "exchange": "binance"},
    {"id": "strat-funding-arb", "name": "Funding Arbitrage", "exchange": "bybit"},
    {"id": "strat-breakout", "name": "Breakout Scanner", "exchange": "okx"},
]

SYMBOLS = ["BTCUSDT", "ETHUSDT"]
SIDES = ["BUY", "SELL"]


def _generate_trades(
    num_days: int,
    trades_per_day: int,
) -> list[dict[str, Any]]:
    """Generate a list of sample trade records."""
    trades: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=num_days)

    for day_offset in range(num_days):
        day_start = start + timedelta(days=day_offset)
        n_trades = max(1, trades_per_day + random.randint(-2, 2))

        for _ in range(n_trades):
            strategy = random.choice(STRATEGIES)
            symbol = random.choice(SYMBOLS)
            side = random.choice(SIDES)
            price = Decimal(str(round(random.uniform(20000, 70000), 2)))
            qty = Decimal(str(round(random.uniform(0.001, 0.1), 6)))
            fee = price * qty * Decimal("0.0004")
            pnl = Decimal(str(round(random.gauss(10, 150), 2)))

            ts = day_start + timedelta(
                hours=random.randint(0, 23),
                minutes=random.randint(0, 59),
                seconds=random.randint(0, 59),
            )

            trades.append(
                {
                    "strategy_id": strategy["id"],
                    "exchange_id": strategy["exchange"],
                    "symbol": symbol,
                    "side": side,
                    "price": price,
                    "quantity": qty,
                    "fee": round(fee, 8),
                    "pnl": pnl,
                    "timestamp": ts,
                }
            )

    return sorted(trades, key=lambda t: t["timestamp"])


def _generate_balance_snapshots(
    num_days: int,
    initial_balance: float = 100_000.0,
) -> list[dict[str, Any]]:
    """Generate daily balance snapshots with random walk."""
    snapshots: list[dict[str, Any]] = []
    now = datetime.now(tz=UTC)
    start = now - timedelta(days=num_days)
    balance = initial_balance
    peak = balance

    for day_offset in range(num_days):
        ts = start + timedelta(days=day_offset, hours=23, minutes=59)
        daily_return = random.gauss(0.001, 0.02)
        balance *= 1 + daily_return
        balance = max(balance, initial_balance * 0.5)  # floor at 50% of initial
        peak = max(peak, balance)
        drawdown_pct = (peak - balance) / peak * 100 if peak > 0 else 0

        snapshots.append(
            {
                "timestamp": ts,
                "total_value": Decimal(str(round(balance, 2))),
                "unrealized_pnl": Decimal(str(round(random.uniform(-500, 500), 2))),
                "realized_pnl": Decimal(str(round(random.uniform(-200, 300), 2))),
                "drawdown_pct": Decimal(str(round(drawdown_pct, 4))),
                "peak_value": Decimal(str(round(peak, 2))),
            }
        )

    return snapshots


def _generate_positions() -> list[dict[str, Any]]:
    """Generate sample open positions."""
    positions: list[dict[str, Any]] = []
    for strategy in STRATEGIES:
        if random.random() < 0.6:
            symbol = random.choice(SYMBOLS)
            direction = random.choice(["LONG", "SHORT"])
            entry_price = Decimal(str(round(random.uniform(25000, 65000), 2)))
            qty = Decimal(str(round(random.uniform(0.01, 0.5), 6)))
            current_price = entry_price * Decimal(str(round(random.uniform(0.95, 1.05), 4)))
            unrealized = (current_price - entry_price) * qty
            if direction == "SHORT":
                unrealized = -unrealized

            positions.append(
                {
                    "strategy_id": strategy["id"],
                    "exchange_id": strategy["exchange"],
                    "symbol": symbol,
                    "direction": direction,
                    "quantity": qty,
                    "avg_entry_price": entry_price,
                    "unrealized_pnl": round(unrealized, 2),
                    "realized_pnl": Decimal(str(round(random.uniform(-1000, 2000), 2))),
                }
            )

    return positions


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS seed_strategies (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    exchange_id TEXT NOT NULL,
    enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS seed_trades (
    id SERIAL PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    exchange_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    price NUMERIC(20, 8) NOT NULL,
    quantity NUMERIC(20, 8) NOT NULL,
    fee NUMERIC(20, 8) NOT NULL,
    pnl NUMERIC(20, 8) NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_balance_snapshots (
    id SERIAL PRIMARY KEY,
    timestamp TIMESTAMPTZ NOT NULL,
    total_value NUMERIC(20, 2) NOT NULL,
    unrealized_pnl NUMERIC(20, 2) NOT NULL,
    realized_pnl NUMERIC(20, 2) NOT NULL,
    drawdown_pct NUMERIC(10, 4) NOT NULL,
    peak_value NUMERIC(20, 2) NOT NULL
);

CREATE TABLE IF NOT EXISTS seed_positions (
    id SERIAL PRIMARY KEY,
    strategy_id TEXT NOT NULL,
    exchange_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity NUMERIC(20, 8) NOT NULL,
    avg_entry_price NUMERIC(20, 8) NOT NULL,
    unrealized_pnl NUMERIC(20, 8) NOT NULL,
    realized_pnl NUMERIC(20, 8) NOT NULL
);
"""

_DROP_TABLES_SQL = """
DROP TABLE IF EXISTS seed_positions CASCADE;
DROP TABLE IF EXISTS seed_balance_snapshots CASCADE;
DROP TABLE IF EXISTS seed_trades CASCADE;
DROP TABLE IF EXISTS seed_strategies CASCADE;
"""


async def _seed(args: argparse.Namespace) -> None:
    """Main seed logic."""
    import asyncpg

    trades = _generate_trades(args.days, args.trades_per_day)
    snapshots = _generate_balance_snapshots(args.days)
    positions = _generate_positions()

    print(
        f"Generated seed data:\n"
        f"  Strategies: {len(STRATEGIES)}\n"
        f"  Trades: {len(trades):,}\n"
        f"  Balance snapshots: {len(snapshots):,}\n"
        f"  Open positions: {len(positions)}\n"
        f"  Period: {args.days} days"
    )

    if args.dry_run:
        print("\nDry run -- no database changes.")
        return

    conn = await asyncpg.connect(args.dsn)
    try:
        if args.drop_existing:
            print("Dropping existing seed tables...")
            await conn.execute(_DROP_TABLES_SQL)

        print("Creating tables...")
        await conn.execute(_CREATE_TABLES_SQL)

        # Insert strategies
        for strat in STRATEGIES:
            await conn.execute(
                "INSERT INTO seed_strategies (id, name, exchange_id) VALUES ($1, $2, $3)"
                " ON CONFLICT (id) DO NOTHING",
                strat["id"],
                strat["name"],
                strat["exchange"],
            )
        print(f"  Inserted {len(STRATEGIES)} strategies")

        # Insert trades in batches
        trade_rows = [
            (
                t["strategy_id"],
                t["exchange_id"],
                t["symbol"],
                t["side"],
                t["price"],
                t["quantity"],
                t["fee"],
                t["pnl"],
                t["timestamp"],
            )
            for t in trades
        ]
        await conn.executemany(
            "INSERT INTO seed_trades"
            " (strategy_id, exchange_id, symbol, side, price, quantity, fee, pnl, timestamp)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)",
            trade_rows,
        )
        print(f"  Inserted {len(trade_rows):,} trades")

        # Insert balance snapshots
        snap_rows = [
            (
                s["timestamp"],
                s["total_value"],
                s["unrealized_pnl"],
                s["realized_pnl"],
                s["drawdown_pct"],
                s["peak_value"],
            )
            for s in snapshots
        ]
        await conn.executemany(
            "INSERT INTO seed_balance_snapshots"
            " (timestamp, total_value, unrealized_pnl, realized_pnl, drawdown_pct, peak_value)"
            " VALUES ($1, $2, $3, $4, $5, $6)",
            snap_rows,
        )
        print(f"  Inserted {len(snap_rows):,} balance snapshots")

        # Insert positions
        pos_rows = [
            (
                p["strategy_id"],
                p["exchange_id"],
                p["symbol"],
                p["direction"],
                p["quantity"],
                p["avg_entry_price"],
                p["unrealized_pnl"],
                p["realized_pnl"],
            )
            for p in positions
        ]
        await conn.executemany(
            "INSERT INTO seed_positions"
            " (strategy_id, exchange_id, symbol, direction,"
            " quantity, avg_entry_price, unrealized_pnl, realized_pnl)"
            " VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
            pos_rows,
        )
        print(f"  Inserted {len(pos_rows)} positions")

        print("\nSeed complete.")

    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    args = _parse_args(argv)
    asyncio.run(_seed(args))


if __name__ == "__main__":
    main()
