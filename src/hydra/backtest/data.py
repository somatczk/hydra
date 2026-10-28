"""Load stored OHLCV bars for backtests, with validation and data provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any, Literal

from hydra.core.types import OHLCV, Timeframe
from hydra.data.errors import NoMarketDataError as NoMarketDataError
from hydra.data.storage import MarketDataRepository

TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M1: 60,
    Timeframe.M5: 300,
    Timeframe.M15: 900,
    Timeframe.H1: 3600,
    Timeframe.H4: 14400,
    Timeframe.D1: 86400,
    Timeframe.W1: 604800,
}


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


@dataclass(frozen=True, slots=True)
class DataProvenance:
    """Which stored bars a backtest actually ran on."""

    exchange: str
    symbol: str
    timeframe: Timeframe
    bar_count: int
    first_ts: datetime
    last_ts: datetime
    gap_count: int
    requested_start: datetime | None = None
    requested_end: datetime | None = None
    data_source: Literal["db"] = "db"

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "bar_count": self.bar_count,
            "first_ts": self.first_ts.isoformat(),
            "last_ts": self.last_ts.isoformat(),
            "gap_count": self.gap_count,
            "requested_start": _iso(self.requested_start),
            "requested_end": _iso(self.requested_end),
            "data_source": self.data_source,
        }


class InvalidMarketDataError(Exception):
    """Stored bars are not strictly increasing in time."""


def validate_bars(bars: list[OHLCV], timeframe: Timeframe) -> int:
    """Raise on duplicate or out-of-order timestamps; return the number of gaps."""
    step = TIMEFRAME_SECONDS[timeframe]
    gaps = 0
    for prev, cur in pairwise(bars):
        delta = (cur.timestamp - prev.timestamp).total_seconds()
        if delta <= 0:
            msg = (
                f"bars not strictly increasing at {cur.timestamp.isoformat()} "
                f"(previous {prev.timestamp.isoformat()})"
            )
            raise InvalidMarketDataError(msg)
        if delta > step:
            gaps += 1
    return gaps


async def no_market_data(
    repo: MarketDataRepository,
    exchange: str,
    symbol: str,
    timeframe: Timeframe,
    start: datetime | None,
    end: datetime | None,
) -> NoMarketDataError:
    """Build the no-data error, filled with the stored range for this exchange/symbol/timeframe."""
    available = await repo.get_bar_range(exchange, symbol, timeframe)
    first, last = (available[0], available[1]) if available else (None, None)
    return NoMarketDataError(exchange, symbol, timeframe, start, end, first, last)


def _provenance(
    bars: list[OHLCV],
    exchange: str,
    symbol: str,
    timeframe: Timeframe,
    start: datetime | None,
    end: datetime | None,
) -> DataProvenance:
    return DataProvenance(
        exchange=exchange,
        symbol=symbol,
        timeframe=timeframe,
        bar_count=len(bars),
        first_ts=bars[0].timestamp,
        last_ts=bars[-1].timestamp,
        gap_count=validate_bars(bars, timeframe),
        requested_start=start,
        requested_end=end,
    )


async def load_bars(
    repo: MarketDataRepository,
    exchange: str,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> tuple[list[OHLCV], DataProvenance]:
    """Stored bars for ``[start, end]`` (inclusive) and their provenance."""
    bars = await repo.get_bars(exchange, symbol, timeframe, start, end)
    if not bars:
        raise await no_market_data(repo, exchange, symbol, timeframe, start, end)
    return bars, _provenance(bars, exchange, symbol, timeframe, start, end)


async def load_latest_bars(
    repo: MarketDataRepository,
    exchange: str,
    symbol: str,
    timeframe: Timeframe,
    limit: int,
) -> tuple[list[OHLCV], DataProvenance]:
    """The most recent ``limit`` stored bars (ascending) and their provenance."""
    bars = await repo.get_latest_bars(exchange, symbol, timeframe, limit)
    if not bars:
        raise await no_market_data(repo, exchange, symbol, timeframe, None, None)
    return bars, _provenance(bars, exchange, symbol, timeframe, None, None)
