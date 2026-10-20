"""Tests for the backtest bar loader: validation, gap counting, provenance, no-data errors."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, cast

import pytest

from hydra.backtest.data import (
    TIMEFRAME_SECONDS,
    DataProvenance,
    InvalidMarketDataError,
    NoMarketDataError,
    load_bars,
    load_latest_bars,
    validate_bars,
)
from hydra.core.types import OHLCV, Timeframe
from hydra.data.storage import MarketDataRepository

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _bar(ts: datetime, close: str = "100") -> OHLCV:
    price = Decimal(close)
    return OHLCV(open=price, high=price, low=price, close=price, volume=Decimal("1"), timestamp=ts)


def _hourly(hours: list[int]) -> list[OHLCV]:
    return [_bar(T0 + timedelta(hours=h)) for h in hours]


class _FakeRepo:
    """Stands in for MarketDataRepository's read methods only."""

    def __init__(
        self, bars: list[OHLCV], stored_range: tuple[datetime, datetime, int] | None
    ) -> None:
        self.bars = bars
        self.stored_range = stored_range
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def get_bars(self, *args: Any) -> list[OHLCV]:
        self.calls.append(("get_bars", args))
        return self.bars

    async def get_latest_bars(self, *args: Any) -> list[OHLCV]:
        self.calls.append(("get_latest_bars", args))
        return self.bars

    async def get_bar_range(self, *args: Any) -> tuple[datetime, datetime, int] | None:
        self.calls.append(("get_bar_range", args))
        return self.stored_range


def _as_repo(fake: _FakeRepo) -> MarketDataRepository:
    return cast("MarketDataRepository", fake)


def test_timeframe_seconds_covers_every_timeframe() -> None:
    assert set(TIMEFRAME_SECONDS) == set(Timeframe)
    assert TIMEFRAME_SECONDS[Timeframe.H4] == 4 * 3600


def test_one_missing_hour_is_one_gap() -> None:
    assert validate_bars(_hourly([0, 1, 3, 4, 5]), Timeframe.H1) == 1


def test_contiguous_bars_have_no_gaps() -> None:
    assert validate_bars(_hourly([0, 1, 2, 3]), Timeframe.H1) == 0


def test_duplicate_timestamp_is_rejected() -> None:
    with pytest.raises(InvalidMarketDataError, match="not strictly increasing"):
        validate_bars(_hourly([0, 1, 1, 2]), Timeframe.H1)


def test_decreasing_timestamp_is_rejected() -> None:
    with pytest.raises(InvalidMarketDataError):
        validate_bars(_hourly([0, 2, 1]), Timeframe.H1)


def test_provenance_to_dict_uses_utc_iso_strings() -> None:
    prov = DataProvenance(
        exchange="binance",
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        bar_count=5,
        first_ts=T0,
        last_ts=T0 + timedelta(hours=5),
        gap_count=1,
        requested_start=T0,
        requested_end=datetime(2026, 1, 1, 23, 59, 59, 999999, tzinfo=UTC),
    )
    assert prov.to_dict() == {
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "bar_count": 5,
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-01T05:00:00+00:00",
        "gap_count": 1,
        "requested_start": "2026-01-01T00:00:00+00:00",
        "requested_end": "2026-01-01T23:59:59.999999+00:00",
        "data_source": "db",
    }


async def test_load_bars_builds_provenance_from_the_bars() -> None:
    bars = _hourly([0, 1, 3, 4, 5])
    repo = _FakeRepo(bars, None)
    end = T0 + timedelta(days=1)
    loaded, prov = await load_bars(_as_repo(repo), "kraken", "BTCUSDT", Timeframe.H1, T0, end)
    assert loaded == bars
    assert repo.calls == [("get_bars", ("kraken", "BTCUSDT", Timeframe.H1, T0, end))]
    assert (prov.exchange, prov.bar_count, prov.gap_count) == ("kraken", 5, 1)
    assert (prov.first_ts, prov.last_ts) == (T0, T0 + timedelta(hours=5))
    assert (prov.requested_start, prov.requested_end) == (T0, end)


async def test_empty_range_reports_what_is_stored() -> None:
    stored = (datetime(2025, 6, 1, tzinfo=UTC), datetime(2025, 7, 1, tzinfo=UTC), 720)
    repo = _FakeRepo([], stored)
    with pytest.raises(NoMarketDataError) as exc:
        await load_bars(
            _as_repo(repo), "binance", "BTCUSDT", Timeframe.H1, T0, T0 + timedelta(days=1)
        )
    detail = exc.value.to_detail()
    assert detail == {
        "code": "no_market_data",
        "message": (
            "No market data for binance BTCUSDT 1h between 2026-01-01T00:00:00+00:00 and "
            "2026-01-02T00:00:00+00:00; available: 2025-06-01T00:00:00+00:00 to "
            "2025-07-01T00:00:00+00:00"
        ),
        "state": "no_data",
        "exchange": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "requested_start": "2026-01-01T00:00:00+00:00",
        "requested_end": "2026-01-02T00:00:00+00:00",
        "available_first_ts": "2025-06-01T00:00:00+00:00",
        "available_last_ts": "2025-07-01T00:00:00+00:00",
    }


async def test_nothing_stored_says_so() -> None:
    repo = _FakeRepo([], None)
    with pytest.raises(NoMarketDataError) as exc:
        await load_latest_bars(_as_repo(repo), "okx", "ETHUSDT", Timeframe.D1, 100)
    assert exc.value.message == "No market data for okx ETHUSDT 1d; available: none stored"
    assert exc.value.to_detail()["available_first_ts"] is None


async def test_invalid_stored_bars_raise() -> None:
    repo = _FakeRepo(_hourly([0, 0]), None)
    with pytest.raises(InvalidMarketDataError):
        await load_latest_bars(_as_repo(repo), "binance", "BTCUSDT", Timeframe.H1, 2)
