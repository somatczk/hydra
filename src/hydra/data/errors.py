"""Market-data availability and fetch errors.

``NoMarketDataError`` is the P0-B no-data vocabulary (code ``no_market_data``, state
``no_data``). It lives in the data layer so the bar loader and the on-demand fetch
engine raise the same class; ``hydra.backtest.data`` re-exports it for every existing
importer. ``DataFetchError`` is the on-demand engine's typed failure.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from hydra.core.types import Timeframe
from hydra.data.backfill import GapRange

FetchFailureReason = Literal["rate_limited", "network", "too_much_data"]


def _iso(ts: datetime | None) -> str | None:
    return ts.isoformat() if ts is not None else None


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


class DataFetchError(Exception):
    """The on-demand fetch gave up; callers map ``reason`` to their retry semantics.

    ``rate_limited`` and ``network`` mean the exchange stayed unreachable past the
    patience deadline — retryable. ``too_much_data`` means the missing bars exceed the
    configured per-job cap — a permanent, fail-fast refusal. ``remaining_ranges`` are
    the gaps still owed when the fetch stopped.
    """

    def __init__(
        self,
        reason: FetchFailureReason,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        *,
        weight_spent: int,
        elapsed_s: float,
        remaining_ranges: list[GapRange],
    ) -> None:
        self.reason = reason
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = timeframe
        self.weight_spent = weight_spent
        self.elapsed_s = elapsed_s
        self.remaining_ranges = remaining_ranges
        super().__init__(self.message)

    @property
    def message(self) -> str:
        where = f"{self.exchange} {self.symbol} {self.timeframe}"
        owed = len(self.remaining_ranges)
        if self.reason == "too_much_data":
            return (
                f"On-demand fetch for {where} refused: the missing bars exceed the "
                f"per-job cap ({owed} range{'s' if owed != 1 else ''})"
            )
        return (
            f"On-demand fetch for {where} failed ({self.reason}) after "
            f"{self.elapsed_s:.1f}s and {self.weight_spent} request weight; "
            f"{owed} range{'s' if owed != 1 else ''} still missing"
        )

    def to_detail(self) -> dict[str, Any]:
        """Body for the shared error envelope (``state`` = ``fetch_failed``)."""
        return {
            "code": "data_fetch_failed",
            "message": self.message,
            "state": "fetch_failed",
            "reason": self.reason,
            "exchange": self.exchange,
            "symbol": self.symbol,
            "timeframe": str(self.timeframe),
            "weight_spent": self.weight_spent,
            "elapsed_s": round(self.elapsed_s, 3),
            "remaining_ranges": [
                {"start": _iso(gap.start), "end": _iso(gap.end)} for gap in self.remaining_ranges
            ],
        }
