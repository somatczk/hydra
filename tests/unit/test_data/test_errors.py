"""Tests for hydra.data.errors — the shared availability/fetch error vocabulary.

``NoMarketDataError`` moved here from ``hydra.backtest.data`` (P6-B1) so the bar
loader and the on-demand engine raise one class; the re-export keeps every
existing ``hydra.backtest.data`` import working.
"""

from __future__ import annotations

from datetime import UTC, datetime

import hydra.backtest.data
from hydra.core.types import Timeframe
from hydra.data.backfill import GapRange
from hydra.data.errors import DataFetchError, NoMarketDataError

D1 = datetime(2026, 3, 1, tzinfo=UTC)
D2 = datetime(2026, 3, 2, tzinfo=UTC)


def test_backtest_data_reexport_is_the_same_class() -> None:
    """``from hydra.backtest.data import NoMarketDataError`` keeps resolving."""
    assert hydra.backtest.data.NoMarketDataError is NoMarketDataError


def test_no_market_data_detail_shape_unchanged() -> None:
    exc = NoMarketDataError("binance", "BTCUSDT", Timeframe.M1, D1, D2, None, None)

    assert exc.message == (
        f"No market data for binance BTCUSDT 1m between {D1.isoformat()} and {D2.isoformat()}; "
        "available: none stored"
    )
    assert exc.to_detail() == {
        "code": "no_market_data",
        "message": exc.message,
        "state": "no_data",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1m",
        "requested_start": D1.isoformat(),
        "requested_end": D2.isoformat(),
        "available_first_ts": None,
        "available_last_ts": None,
    }


def test_data_fetch_error_rate_limited_detail() -> None:
    exc = DataFetchError(
        "rate_limited",
        "binance",
        "ETHUSDT",
        Timeframe.H1,
        weight_spent=84,
        elapsed_s=912.5,
        remaining_ranges=[GapRange(start=D1, end=D2)],
    )

    assert exc.message == (
        "On-demand fetch for binance ETHUSDT 1h failed (rate_limited) after "
        "912.5s and 84 request weight; 1 range still missing"
    )
    assert exc.to_detail() == {
        "code": "data_fetch_failed",
        "message": exc.message,
        "state": "fetch_failed",
        "reason": "rate_limited",
        "exchange": "binance",
        "symbol": "ETHUSDT",
        "timeframe": "1h",
        "weight_spent": 84,
        "elapsed_s": 912.5,
        "remaining_ranges": [{"start": D1.isoformat(), "end": D2.isoformat()}],
    }


def test_data_fetch_error_too_much_data_message() -> None:
    exc = DataFetchError(
        "too_much_data",
        "binance",
        "ETHUSDT",
        Timeframe.H1,
        weight_spent=0,
        elapsed_s=0.0,
        remaining_ranges=[GapRange(start=D1, end=D1), GapRange(start=D2, end=D2)],
    )

    assert exc.message == (
        "On-demand fetch for binance ETHUSDT 1h refused: the missing bars exceed the "
        "per-job cap (2 ranges)"
    )
