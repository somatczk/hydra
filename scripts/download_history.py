"""Bulk download BTC/USDT 1m historical data from exchanges.

Downloads OHLCV candle data via CCXT and stores it directly into
TimescaleDB through the MarketDataRepository.  Supports resumable
downloads (checks for existing data) and rate limiting.

Usage::

    python -m scripts.download_history \\
        --exchange binance \\
        --symbol BTCUSDT \\
        --start 2020-01-01 \\
        --end 2026-01-01

"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import UTC, datetime


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bulk download historical OHLCV data and store in TimescaleDB.",
    )
    parser.add_argument(
        "--exchange",
        type=str,
        default="binance",
        help="Exchange ID (default: binance)",
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="BTCUSDT",
        help="Trading pair symbol (default: BTCUSDT)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2020-01-01",
        help="Start date in YYYY-MM-DD format (default: 2020-01-01)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="2026-01-01",
        help="End date in YYYY-MM-DD format (default: 2026-01-01)",
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default="1m",
        help="Candle timeframe (default: 1m)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Number of candles per CCXT request (default: 1000)",
    )
    parser.add_argument(
        "--rate-limit-ms",
        type=int,
        default=100,
        help="Minimum milliseconds between API calls (default: 100)",
    )
    parser.add_argument(
        "--dsn",
        type=str,
        default="postgresql://hydra:hydra_dev@localhost:5432/hydra",
        help="TimescaleDB connection DSN",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print plan without downloading or storing data",
    )
    return parser.parse_args(argv)


def _parse_date(date_str: str) -> datetime:
    """Parse a YYYY-MM-DD string into a UTC datetime."""
    return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)


async def _download(args: argparse.Namespace) -> None:
    """Core async download loop."""
    import ccxt.async_support as ccxt_async

    from hydra.core.types import OHLCV, Timeframe
    from hydra.data.storage import MarketDataRepository

    exchange_cls = getattr(ccxt_async, args.exchange, None)
    if exchange_cls is None:
        print(f"ERROR: Exchange '{args.exchange}' not supported by CCXT")
        sys.exit(1)

    exchange = exchange_cls({"enableRateLimit": True})

    start_dt = _parse_date(args.start)
    end_dt = _parse_date(args.end)
    since_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)
    timeframe = Timeframe(args.timeframe)

    # Connect to repository
    repo = MarketDataRepository(dsn=args.dsn)

    if not args.dry_run:
        await repo.connect()

    # Check existing data for resume support
    resume_from_ms = since_ms
    if not args.dry_run:
        latest = await repo.get_latest_bar(args.exchange, args.symbol, timeframe)
        if latest is not None:
            latest_ms = int(latest.timestamp.timestamp() * 1000)
            if latest_ms >= since_ms:
                resume_from_ms = latest_ms + 60_000  # next minute
                resume_ts = datetime.fromtimestamp(resume_from_ms / 1000, tz=UTC).isoformat()
                print(
                    f"Resuming from {resume_ts}"
                    f" (found existing data up to"
                    f" {latest.timestamp.isoformat()})"
                )

    total_minutes = (end_ms - resume_from_ms) // 60_000
    if total_minutes <= 0:
        print("No data to download -- range already covered.")
        if not args.dry_run:
            await repo.close()
        await exchange.close()
        return

    total_batches = (total_minutes + args.batch_size - 1) // args.batch_size

    print(
        f"Downloading {args.symbol} {args.timeframe} from {args.exchange}\n"
        f"  Range: {datetime.fromtimestamp(resume_from_ms / 1000, tz=UTC).isoformat()}"
        f" -> {end_dt.isoformat()}\n"
        f"  Estimated candles: {total_minutes:,}\n"
        f"  Batches: {total_batches:,} (batch size: {args.batch_size})"
    )

    if args.dry_run:
        print("Dry run -- exiting.")
        await exchange.close()
        return

    from decimal import Decimal

    current_ms = resume_from_ms
    downloaded = 0
    stored = 0
    batch_num = 0
    t_start = time.monotonic()

    try:
        while current_ms < end_ms:
            batch_num += 1
            t_batch = time.monotonic()

            ohlcv_raw = await exchange.fetch_ohlcv(
                args.symbol,
                timeframe=args.timeframe,
                since=current_ms,
                limit=args.batch_size,
            )

            if not ohlcv_raw:
                print(f"\n  No more data returned at batch {batch_num}.")
                break

            bars: list[tuple[str, str, Timeframe, OHLCV]] = []
            for candle in ohlcv_raw:
                ts_ms, o, h, low, c, v = candle[:6]
                bars.append(
                    (
                        args.exchange,
                        args.symbol,
                        timeframe,
                        OHLCV(
                            open=Decimal(str(o)),
                            high=Decimal(str(h)),
                            low=Decimal(str(low)),
                            close=Decimal(str(c)),
                            volume=Decimal(str(v)),
                            timestamp=datetime.fromtimestamp(ts_ms / 1000, tz=UTC),
                        ),
                    )
                )

            n_stored = await repo.store_bars(bars)
            downloaded += len(ohlcv_raw)
            stored += n_stored

            # Advance cursor past the last candle
            last_ts_ms = int(ohlcv_raw[-1][0])
            current_ms = last_ts_ms + 60_000

            # Progress
            pct = min(100.0, (current_ms - resume_from_ms) / (end_ms - resume_from_ms) * 100)
            elapsed = time.monotonic() - t_start
            rate = downloaded / elapsed if elapsed > 0 else 0
            batch_dur = time.monotonic() - t_batch

            sys.stdout.write(
                f"\r  [{pct:5.1f}%] batch {batch_num}/{total_batches}"
                f"  downloaded={downloaded:,}  stored={stored:,}"
                f"  rate={rate:.0f} candles/s  batch_time={batch_dur:.2f}s"
            )
            sys.stdout.flush()

            # Rate limiting
            sleep_s = args.rate_limit_ms / 1000.0 - batch_dur
            if sleep_s > 0:
                await asyncio.sleep(sleep_s)

    finally:
        await exchange.close()
        await repo.close()

    elapsed = time.monotonic() - t_start
    print(
        f"\n\nDone. Downloaded {downloaded:,} candles, stored {stored:,}"
        f" in {elapsed:.1f}s ({downloaded / elapsed:.0f} candles/s)"
    )


def main(argv: list[str] | None = None) -> None:
    """Entry point."""
    args = _parse_args(argv)
    asyncio.run(_download(args))


if __name__ == "__main__":
    main()
