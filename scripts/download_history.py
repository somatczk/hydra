"""Fill every missing OHLCV range in TimescaleDB from Binance spot REST; safe to re-run.

Gaps are found with one SQL query over ``ts.ohlcv_1m`` and only those ranges are fetched
(retry with backoff, forming candle never stored).

Usage::

    python scripts/download_history.py --start 2020-01-01 --dsn "$DATABASE_URL" --dry-run

"""

from __future__ import annotations

import argparse
import asyncio
import functools
from datetime import UTC, datetime


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fill every missing OHLCV range; safe to re-run.")
    parser.add_argument("--exchange", default="binance", help="Exchange ID (default: binance)")
    parser.add_argument("--symbol", default="BTCUSDT", help="Symbol (default: BTCUSDT)")
    parser.add_argument("--timeframe", default="1m", help="Candle timeframe (default: 1m)")
    parser.add_argument("--start", default="2020-01-01", help="First day, YYYY-MM-DD (UTC)")
    parser.add_argument(
        "--end", help="Last bar open time, YYYY-MM-DD 00:00 UTC inclusive (default: newest closed)"
    )
    parser.add_argument(
        "--dsn",
        default="postgresql://hydra:hydra_dev@localhost:5432/hydra",
        help="TimescaleDB connection DSN",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="List the missing ranges; fetch nothing"
    )
    return parser.parse_args(argv)


def _parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)


async def _download(args: argparse.Namespace) -> int:
    """With ``--dry-run`` print the missing ranges, else fill them; return the bars fetched."""
    from hydra.core.types import Timeframe
    from hydra.data.backfill import ExchangeBackfillService
    from hydra.data.bars import last_closed_open, timeframe_seconds
    from hydra.data.storage import MarketDataRepository
    from hydra.execution import client_factory

    exchange_id = client_factory.validate_venue(args.exchange)
    timeframe = Timeframe(args.timeframe)
    start = _parse_date(args.start)
    end = _parse_date(args.end) if args.end else last_closed_open(datetime.now(UTC), timeframe)

    repo = MarketDataRepository(dsn=args.dsn)
    await repo.connect()
    service = ExchangeBackfillService(
        repo,
        exchange_factories={
            exchange_id: functools.partial(client_factory.rest_exchange_factory, exchange_id)
        },
    )
    try:
        if args.dry_run:
            step = timeframe_seconds(timeframe)
            gaps = await service.detect_gaps(exchange_id, args.symbol, timeframe, start, end)
            for gap in gaps:
                bars = int((gap.end - gap.start).total_seconds()) // step + 1
                print(f"missing {gap.start.isoformat()} -> {gap.end.isoformat()} ({bars} bars)")
            print(f"{len(gaps)} missing ranges; dry run, nothing fetched")
            return 0
        total = await service.backfill_gaps(exchange_id, args.symbol, timeframe, start, end)
        print(f"fetched and stored {total} bars")
        return total
    finally:
        await service.close()
        await repo.close()


def main(argv: list[str] | None = None) -> None:
    asyncio.run(_download(_parse_args(argv)))


if __name__ == "__main__":
    main()
