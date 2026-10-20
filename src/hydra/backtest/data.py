"""Load stored OHLCV bars for backtests, with validation and data provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Any, Literal

from hydra.core.types import OHLCV, Timeframe
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


class NoMarketDataError(Exception):
    """No stored bars for the exchange/symbol/timeframe in the requested range."""

    def __init__(
        self,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        requested_start: datetime | None,
        requested_end: datetime | None,
        available_first_ts: datetime | None,
        available_last_ts: datetime | None,
    ) -> None:
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self.requested_start = requested_start
        self.requested_end = requested_end
        self.available_first_ts = available_first_ts
        self.available_last_ts = available_last_ts
        super().__init__(self.message)

    @property
    def message(self) -> str:
        where = f"{self.exchange} {self.symbol} {self.timeframe}"
        if self.requested_start is not None and self.requested_end is not None:
            wanted = f" between {_iso(self.requested_start)} and {_iso(self.requested_end)}"
        else:
            wanted = ""
        if self.available_first_ts is not None and self.available_last_ts is not None:
            available = (
                f"available: {_iso(self.available_first_ts)} to {_iso(self.available_last_ts)}"
            )
        else:
            available = "available: none stored"
        return f"No market data for {where}{wanted}; {available}"

    def to_detail(self) -> dict[str, Any]:
        """Body for the shared error envelope (``state`` = ``no_data``)."""
        return {
            "code": "no_market_data",
            "message": self.message,
            "state": "no_data",
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "requested_start": _iso(self.requested_start),
            "requested_end": _iso(self.requested_end),
            "available_first_ts": _iso(self.available_first_ts),
            "available_last_ts": _iso(self.available_last_ts),
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
