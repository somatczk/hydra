"""Bar-grid helpers and ClosedBarAssembler, fed through the real ccxt.pro kline parser."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.bars import (
    ClosedBarAssembler,
    ceil_to_tf,
    close_time,
    floor_to_tf,
    last_closed_open,
    timeframe_seconds,
)
from hydra.data.normalizer import DataNormalizer
from tests.fixtures.binance_kline import HOUR_MS, BinanceKlineDriver

T0_MS = 1_760_000_400_000  # 2025-10-09T09:00:00Z, on the hour
T0 = datetime.fromtimestamp(T0_MS / 1000, tz=UTC)
T1_MS = T0_MS + HOUR_MS


def _rows(result: dict[str, dict[str, list[list[float]]]]) -> list[OHLCV]:
    norm = DataNormalizer()
    return [norm.normalize_ohlcv(r, "binance") for r in result["BTC/USDT"]["1h"]]


def _bar(ts: datetime, close: str) -> OHLCV:
    c = Decimal(close)
    return OHLCV(open=c, high=c, low=c, close=c, volume=Decimal(1), timestamp=ts)


class TestGrid:
    def test_timeframe_seconds(self) -> None:
        assert timeframe_seconds(Timeframe.H4) == 14_400
        with pytest.raises(ValueError, match="Unknown timeframe"):
            timeframe_seconds("3m")

    def test_floor_and_ceil(self) -> None:
        t = datetime(2026, 1, 1, 13, 17, 5, 123, tzinfo=UTC)
        assert floor_to_tf(t, Timeframe.H1) == datetime(2026, 1, 1, 13, tzinfo=UTC)
        assert floor_to_tf(t, Timeframe.H4) == datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert ceil_to_tf(t, Timeframe.H1) == datetime(2026, 1, 1, 14, tzinfo=UTC)
        on_grid = datetime(2026, 1, 1, 12, tzinfo=UTC)
        assert ceil_to_tf(on_grid, Timeframe.H4) == on_grid

    def test_weekly_grid_opens_on_monday(self) -> None:
        thursday = datetime(2026, 1, 1, 10, tzinfo=UTC)
        assert floor_to_tf(thursday, Timeframe.W1) == datetime(2025, 12, 29, tzinfo=UTC)
        assert floor_to_tf(thursday, Timeframe.W1).weekday() == 0

    def test_close_time_and_last_closed_open(self) -> None:
        assert close_time(T0, Timeframe.H1) == T0 + timedelta(hours=1)
        now = T0 + timedelta(minutes=30)
        assert last_closed_open(now, Timeframe.H1) == T0 - timedelta(hours=1)
        assert last_closed_open(T0, Timeframe.H1) == T0 - timedelta(hours=1)


class TestClosedBarAssembler:
    def test_forming_updates_emit_one_closed_bar(self) -> None:
        driver = BinanceKlineDriver()
        asm = ClosedBarAssembler(Timeframe.H1)
        pushes = [
            (T0_MS, (100, 101, 99, 100.5, 1), False),
            (T0_MS, (100, 102, 99, 101.7, 2), False),
            (T0_MS, (100, 102, 98, 99.2, 3), True),
            (T1_MS, (99.2, 99.5, 99, 99.3, 1), False),
            (T1_MS, (99.2, 100, 99, 99.9, 2), False),
        ]
        emitted_per_push: list[list[OHLCV]] = []
        prices: list[Decimal | None] = []
        for open_ms, ohlcv, closed in pushes:
            result = driver.push_and_read(
                "BTCUSDT", "BTC/USDT", "1h", open_ms, ohlcv, closed=closed
            )
            out = asm.feed(_rows(result))
            emitted_per_push.append(out.closed)
            prices.append(out.latest_price)

        assert [len(e) for e in emitted_per_push] == [0, 0, 0, 1, 0]
        bar = emitted_per_push[3][0]
        assert bar.timestamp == T0
        assert (bar.open, bar.high, bar.low, bar.close, bar.volume) == (
            Decimal("100"),
            Decimal("102"),
            Decimal("98"),
            Decimal("99.2"),
            Decimal("3"),
        )
        assert prices[3:] == [Decimal("99.3"), Decimal("99.9")]
        assert asm.last_emitted == T0

    def test_batched_return(self) -> None:
        driver = BinanceKlineDriver()
        driver.push("BTCUSDT", "1h", T0_MS, (100, 101, 99, 100.5, 1))
        driver.push("BTCUSDT", "1h", T0_MS, (100, 102, 98, 99.2, 3), closed=True)
        driver.push("BTCUSDT", "1h", T1_MS, (99.2, 99.5, 99, 99.3, 1))
        result = driver.read("BTC/USDT", "1h")
        assert [r[0] for r in result["BTC/USDT"]["1h"]] == [T0_MS, T1_MS]

        asm = ClosedBarAssembler(Timeframe.H1)
        out = asm.feed(_rows(result))
        assert [(b.timestamp, b.close) for b in out.closed] == [(T0, Decimal("99.2"))]
        assert out.latest_price == Decimal("99.3")
        assert asm.feed(_rows(result)).closed == []

    def test_replay_after_reconnect_emits_nothing(self) -> None:
        asm = ClosedBarAssembler(Timeframe.H1)
        t1 = T0 + timedelta(hours=1)
        assert asm.feed([_bar(T0, "100"), _bar(t1, "101")]).closed == [_bar(T0, "100")]
        assert asm.held == _bar(t1, "101")

        replay = asm.feed([_bar(T0, "100"), _bar(T0, "100.5")])
        assert replay.closed == []
        assert replay.latest_price is None

        asm.reset_after_reconnect()
        assert asm.held is None
        assert asm.last_emitted == T0

    def test_seeded_from_warmup(self) -> None:
        tail = T0 + timedelta(hours=2)
        asm = ClosedBarAssembler(Timeframe.H1, last_emitted=tail)
        old = [_bar(T0 + timedelta(hours=h), "100") for h in range(3)]
        out = asm.feed(old)
        assert out.closed == []
        assert asm.held is None

        t3, t4 = tail + timedelta(hours=1), tail + timedelta(hours=2)
        assert asm.feed([_bar(t3, "101")]).closed == []
        assert asm.feed([_bar(t4, "102")]).closed == [_bar(t3, "101")]

    def test_seed_drops_superseded_held_candle(self) -> None:
        asm = ClosedBarAssembler(Timeframe.H1)
        asm.feed([_bar(T0, "100")])
        asm.seed(T0)
        assert asm.held is None
        asm.seed(T0 - timedelta(hours=5))
        assert asm.last_emitted == T0
