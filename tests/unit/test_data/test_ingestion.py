"""ExchangeFeedManager: closed bars once, one watch loop, isolation, watchdog, gap fill, MTF.

Candle updates go through the real ``ccxt.pro.binance.handle_ohlcv`` (BinanceKlineDriver),
so the feed consumes exactly the shape ``watch_ohlcv_for_symbols`` returns.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import ccxt
import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, Event, FeedStatusEvent, PriceEvent
from hydra.core.types import OHLCV, Timeframe
from hydra.data import ingestion
from hydra.data.bars import last_closed_open
from hydra.data.ingestion import ExchangeFeedManager, InsufficientHistoryError
from tests.fixtures.binance_kline import (
    BinanceKlineDriver,
    FakeRestExchange,
    FakeWsExchange,
)

DAY = datetime(2026, 3, 2, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)


def _ms(ts: datetime) -> int:
    return int(ts.timestamp() * 1000)


def _row(ts: datetime, close: float, *, high: float | None = None, low: float | None = None):
    h = high if high is not None else close + 1
    lo = low if low is not None else close - 1
    return [_ms(ts), close, h, lo, close, 10.0]


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Iterator[None]:
    metrics._reset_metrics()
    ingestion._STREAM_OWNERS.clear()
    yield
    metrics._reset_metrics()
    ingestion._STREAM_OWNERS.clear()


class Clock:
    def __init__(self, at: datetime) -> None:
        self.t = at.timestamp()

    def __call__(self) -> float:
        return self.t

    def set(self, at: datetime) -> None:
        self.t = at.timestamp()


async def _fast_sleep(seconds: float) -> None:
    await asyncio.sleep(0)


async def _throttled_sleep(seconds: float) -> None:
    await asyncio.sleep(0.005)


async def until(cond: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        if loop.time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.002)


class Recorder:
    def __init__(self) -> None:
        self.events: list[Event] = []

    async def __call__(self, event: Event) -> None:
        self.events.append(event)

    @property
    def bars(self) -> list[BarEvent]:
        return [e for e in self.events if isinstance(e, BarEvent)]

    @property
    def statuses(self) -> list[FeedStatusEvent]:
        return [e for e in self.events if isinstance(e, FeedStatusEvent)]

    @property
    def prices(self) -> list[PriceEvent]:
        return [e for e in self.events if isinstance(e, PriceEvent)]


class MemoryRepo:
    """MarketDataRepository stand-in keyed like ts.ohlcv_1m."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, datetime], OHLCV] = {}
        self.upserts: list[list[tuple[str, str, Timeframe, OHLCV]]] = []
        self.fail_next = 0

    def seed(self, symbol: str, tf: Timeframe, bars: list[OHLCV]) -> None:
        for b in bars:
            self.rows[("binance", symbol, str(tf), b.timestamp)] = b

    async def upsert_closed_bars(
        self, bars: list[tuple[str, str, Timeframe, OHLCV]]
    ) -> tuple[int, int]:
        if self.fail_next:
            self.fail_next -= 1
            raise ConnectionError("db down")
        self.upserts.append(list(bars))
        for e, s, tf, b in bars:
            self.rows[(e, s, str(tf), b.timestamp)] = b
        return (len(bars), 0)

    async def get_latest_bars(
        self, exchange_id: str, symbol: str, tf: Timeframe, limit: int, end: datetime | None = None
    ) -> list[OHLCV]:
        keys = sorted(
            k
            for k in self.rows
            if k[:3] == (exchange_id, symbol, str(tf)) and (end is None or k[3] <= end)
        )
        return [self.rows[k] for k in keys[-limit:]]

    @property
    def persisted(self) -> list[tuple[str, str, Timeframe, OHLCV]]:
        return [row for batch in self.upserts for row in batch]


class Harness:
    def __init__(
        self,
        clock: Clock,
        *,
        repo: MemoryRepo | None = None,
        rest: FakeRestExchange | None = None,
        sleep: Callable[[float], Any] = _fast_sleep,
        **kwargs: Any,
    ) -> None:
        self.bus = InMemoryEventBus()
        self.recorder = Recorder()
        self.ws: list[FakeWsExchange] = []
        self.rest = rest or FakeRestExchange()
        self.clock = clock
        self.driver = BinanceKlineDriver()
        options: dict[str, Any] = {
            "ws_silence_s": 1e6,
            "stale_after_bars": 10_000,
            "watchdog_interval_s": 0.005,
            "mtf_barrier_s": 5.0,
            "publish_replay": True,
        }
        options.update(kwargs)
        self.feed = ExchangeFeedManager(
            event_bus=self.bus,
            exchange_factories={"binance": self._new_ws},
            repository=repo,
            rest_factory=lambda _eid: self.rest,
            clock=clock,
            sleep=sleep,
            **options,
        )

    def _new_ws(self) -> FakeWsExchange:
        ws = FakeWsExchange()
        self.ws.append(ws)
        return ws

    async def start(
        self, symbols: list[str] | None = None, timeframes: list[Timeframe] | None = None
    ) -> None:
        for event_type in ("bar", "price", "feed_status"):
            await self.bus.subscribe(event_type, self.recorder)
        await self.feed.connect("binance", symbols or ["BTCUSDT"], timeframes or [Timeframe.H1])

    def kline(
        self,
        ts: datetime,
        close: float,
        *,
        tf: str = "1h",
        market: str = "BTCUSDT",
        high: float | None = None,
        low: float | None = None,
        ws: int = -1,
    ) -> None:
        unified = {"BTCUSDT": "BTC/USDT", "ETHUSDT": "ETH/USDT"}[market]
        h = high if high is not None else close + 1
        lo = low if low is not None else close - 1
        result = self.driver.push_and_read(
            market, unified, tf, _ms(ts), (close, h, lo, close, 10.0)
        )
        self.ws[ws].feed(result)

    async def stop(self) -> None:
        await self.feed.disconnect_all()


def _gauge(name: str, symbol: str = "BTCUSDT", timeframe: str = "1h") -> float | None:
    return REGISTRY.get_sample_value(
        name, {"exchange_id": "binance", "symbol": symbol, "timeframe": timeframe}
    )


# ---------------------------------------------------------------------------
# Symbols and the closed-bar path
# ---------------------------------------------------------------------------


async def test_unified_symbol_maps_back_to_config_id() -> None:
    clock = Clock(DAY + H + 5 * M)
    repo = MemoryRepo()
    h = Harness(clock, repo=repo)
    await h.start()
    h.kline(DAY, 100.5)
    h.kline(DAY, 101.0)
    h.kline(DAY + H, 101.2)
    await until(lambda: len(h.recorder.bars) == 1 and len(repo.persisted) == 1)

    event = h.recorder.bars[0]
    assert event.symbol == "BTCUSDT"
    assert event.timeframe == Timeframe.H1
    assert event.exchange_id == "binance"
    assert event.replay is False
    assert event.ohlcv is not None and event.ohlcv.close == Decimal("101.0")
    assert repo.persisted == [("binance", "BTCUSDT", Timeframe.H1, event.ohlcv)]
    assert h.ws[0].subscriptions[0] == [["BTC/USDT", "1h"]]
    assert _gauge("hydra_feed_last_message_timestamp_seconds") == clock.t
    assert _gauge("hydra_feed_last_closed_bar_timestamp_seconds") == DAY.timestamp()
    assert _gauge("hydra_feed_bar_interval_seconds") == 3600
    await h.stop()
    assert _gauge("hydra_feed_last_message_timestamp_seconds") is None
    assert _gauge("hydra_feed_bar_interval_seconds") is None


@pytest.mark.parametrize("symbol", ["DOGEUSDT", "BTC/USDT:USDT"])
async def test_unknown_or_non_spot_symbol_raises_at_connect(symbol: str) -> None:
    h = Harness(Clock(DAY))
    with pytest.raises(ValueError, match="spot market"):
        await h.feed.connect("binance", [symbol], [Timeframe.H1])
    assert h.ws[0].close_calls == 1
    assert "binance" not in h.feed._exchanges


async def test_unified_config_symbol_is_kept_as_given() -> None:
    clock = Clock(DAY + H + M)
    h = Harness(clock)
    await h.start(symbols=["BTC/USDT"])
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 100.0)
    await until(lambda: len(h.recorder.bars) == 1)
    assert h.recorder.bars[0].symbol == "BTC/USDT"
    await h.stop()


async def test_subscriber_exception_isolated() -> None:
    clock = Clock(DAY + 3 * H + M)
    h = Harness(clock)

    async def failing(event: Event) -> None:
        raise RuntimeError("strategy bug")

    await h.bus.subscribe("bar", failing)
    await h.start()
    for i in range(4):
        h.kline(DAY + i * H, 100.0 + i)
    await until(lambda: len(h.recorder.bars) == 3)

    assert [b.ohlcv.timestamp for b in h.recorder.bars if b.ohlcv] == [
        DAY,
        DAY + H,
        DAY + 2 * H,
    ]
    assert len(h.ws) == 1
    assert h.bus.handler_errors == 3
    assert (
        REGISTRY.get_sample_value("hydra_ws_reconnects_total", {"exchange_id": "binance"}) is None
    )
    await h.stop()


async def test_slow_consumer_does_not_block_ws() -> None:
    clock = Clock(DAY + 6 * H + M)
    h = Harness(clock)
    gate = asyncio.Event()
    received: list[datetime] = []

    async def slow(event: Event) -> None:
        await gate.wait()
        assert isinstance(event, BarEvent) and event.ohlcv is not None
        received.append(event.ohlcv.timestamp)

    await h.bus.subscribe("bar", slow)
    await h.start()
    for i in range(7):
        h.kline(DAY + i * H, 100.0 + i)
    await until(lambda: h.ws[0].watch_calls >= 7)
    assert received == []
    assert h.ws[0].watch_calls >= 5

    gate.set()
    await until(lambda: len(received) == 6)
    assert received == [DAY + i * H for i in range(6)]
    await h.stop()


async def test_forming_price_is_throttled_to_one_per_second() -> None:
    clock = Clock(DAY + 10 * M)
    h = Harness(clock)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY, 100.5)
    await until(lambda: h.ws[0].watch_calls >= 3)
    clock.t += 1.5
    h.kline(DAY, 101.0)
    await until(lambda: len(h.recorder.prices) == 2)
    assert [p.price for p in h.recorder.prices] == [Decimal("100.0"), Decimal("101.0")]
    assert h.recorder.prices[0].symbol == "BTCUSDT"
    assert h.recorder.bars == []
    await h.stop()


async def test_invalid_bar_rejected_and_spike_counted() -> None:
    clock = Clock(DAY + 3 * H + M)
    repo = MemoryRepo()
    h = Harness(clock, repo=repo)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 125.0)
    h.kline(DAY + 2 * H, 100.0, high=90.0, low=110.0)
    h.kline(DAY + 3 * H, 100.0)
    await until(lambda: h.ws[0].watch_calls >= 5)
    await until(lambda: len(repo.persisted) == 2)

    assert [b.ohlcv.close for b in h.recorder.bars if b.ohlcv] == [
        Decimal("100.0"),
        Decimal("125.0"),
    ]
    assert [b.timestamp for *_, b in repo.persisted] == [DAY, DAY + H]
    rejected = REGISTRY.get_sample_value(
        "hydra_feed_bars_rejected_total",
        {"exchange_id": "binance", "symbol": "BTCUSDT", "reason": "ohlc_inconsistent"},
    )
    assert rejected == 1.0
    spikes = REGISTRY.get_sample_value(
        "hydra_feed_anomalies_total",
        {"exchange_id": "binance", "symbol": "BTCUSDT", "kind": "price_spike"},
    )
    assert spikes == 1.0
    ws_bars = REGISTRY.get_sample_value(
        "hydra_feed_bars_total",
        {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h", "source": "ws"},
    )
    assert ws_bars == 2.0
    await h.stop()


async def test_persist_failure_does_not_block_dispatch() -> None:
    clock = Clock(DAY + H + M)
    repo = MemoryRepo()
    repo.fail_next = 1
    h = Harness(clock, repo=repo)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 100.0)
    await until(lambda: len(h.recorder.bars) == 1)
    await until(lambda: len(repo.persisted) == 1)

    errors = REGISTRY.get_sample_value(
        "hydra_feed_persist_errors_total", {"exchange_id": "binance"}
    )
    assert errors == 1.0
    assert repo.persisted[0][3].timestamp == DAY
    await h.stop()


# ---------------------------------------------------------------------------
# Reconnect, watchdog and gap fill
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("close_wakes_watch", [True, False])
async def test_stall_marks_stale_reconnects_and_fills_gap(close_wakes_watch: bool) -> None:
    clock = Clock(DAY + H + 10 * M)
    repo = MemoryRepo()
    rest = FakeRestExchange([_row(DAY + i * H, 200.0 + i) for i in range(5)])
    h = Harness(clock, repo=repo, rest=rest, ws_silence_s=60.0)
    await h.start()
    h.ws[0].wake_on_close = close_wakes_watch
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)
    last_message = clock.t

    clock.set(DAY + 3 * H + 10 * M)
    await until(lambda: len(h.ws) == 2)
    assert _gauge("hydra_feed_last_message_timestamp_seconds") == last_message
    assert clock.t - last_message > 120
    assert h.ws[0].close_calls == 1

    stale = h.recorder.statuses
    assert [(s.status, s.reason, s.symbol, s.timeframe) for s in stale] == [
        ("stale", "ws_silence", "", None)
    ]
    assert stale[0].age_seconds >= 60

    await until(lambda: len(h.recorder.bars) == 3)
    h.kline(DAY + 3 * H, 103.0)
    await until(lambda: len(h.recorder.statuses) == 2)

    tail = h.recorder.events[h.recorder.events.index(stale[0]) + 1 :]
    summary = [
        (e.ohlcv.timestamp, e.replay) if isinstance(e, BarEvent) and e.ohlcv else e.status
        for e in tail
        if isinstance(e, BarEvent | FeedStatusEvent)
    ]
    assert summary == [(DAY + H, True), (DAY + 2 * H, False), "live"]
    gap_bar = h.recorder.bars[1].ohlcv
    assert gap_bar is not None and gap_bar.close == Decimal("201.0")
    assert REGISTRY.get_sample_value("hydra_ws_reconnects_total", {"exchange_id": "binance"}) == 1.0
    assert h.ws[1].set_markets_calls == 1
    assert h.ws[1].load_markets_calls == 0
    await until(lambda: len(repo.persisted) == 3)
    assert [b.timestamp for *_, b in repo.persisted] == [DAY, DAY + H, DAY + 2 * H]
    rest_gap = REGISTRY.get_sample_value(
        "hydra_feed_bars_total",
        {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h", "source": "rest_gap"},
    )
    assert rest_gap == 2.0
    await h.stop()


async def test_bar_overdue_stale_and_recovery() -> None:
    clock = Clock(DAY + 30 * 1_000_000 * timedelta(microseconds=1))
    h = Harness(clock, stale_after_bars=2)
    await h.start(timeframes=[Timeframe.M1])
    for second in range(0, 240, 20):
        clock.set(DAY + timedelta(seconds=30 + second))
        h.kline(DAY, 100.0, tf="1m")
        await until(lambda n=h.ws[0].watch_calls: h.ws[0].watch_calls > n)
    await until(lambda: len(h.recorder.statuses) == 1)

    status = h.recorder.statuses[0]
    assert (status.status, status.reason, status.symbol, status.timeframe) == (
        "stale",
        "bar_overdue",
        "BTCUSDT",
        Timeframe.M1,
    )
    assert h.feed.stale_reasons("binance") == {"BTCUSDT/1m": ["bar_overdue"]}
    assert h.recorder.bars == []

    clock.set(DAY + 3 * M + 50 * timedelta(seconds=1))
    h.kline(DAY + M, 100.0, tf="1m")
    h.kline(DAY + 2 * M, 100.0, tf="1m")
    await until(lambda: len(h.recorder.statuses) == 2)
    assert h.recorder.statuses[1].status == "live"
    assert h.feed.stale_reasons("binance") == {}
    await h.stop()


async def test_recreate_once_per_threshold() -> None:
    h = Harness(Clock(DAY))
    await h.start(symbols=["BTCUSDT", "ETHUSDT"], timeframes=[Timeframe.H1, Timeframe.H4])
    assert [t.get_name() for t in h.feed._tasks["binance"]] == [
        "ohlcv-binance",
        "dispatch-binance",
        "watchdog-binance",
        "gaps-binance",
    ]
    h.ws[0].feed(*[ConnectionError(f"drop {i}") for i in range(5)])
    await until(lambda: len(h.ws) == 2)
    h.ws[1].feed(*[ConnectionError(f"drop {i}") for i in range(5)])
    await until(lambda: len(h.ws) == 3)
    await until(lambda: h.ws[2].watch_calls == 1)
    await asyncio.sleep(0.05)

    assert len(h.ws) == 3
    assert [ws.watch_calls for ws in h.ws] == [5, 5, 1]
    assert h.ws[2].subscriptions[0] == [
        ["BTC/USDT", "1h"],
        ["BTC/USDT", "4h"],
        ["ETH/USDT", "1h"],
        ["ETH/USDT", "4h"],
    ]
    assert REGISTRY.get_sample_value("hydra_ws_reconnects_total", {"exchange_id": "binance"}) == 2.0
    await h.stop()


async def test_hole_in_ws_stream_is_filled_over_rest() -> None:
    clock = Clock(DAY + 4 * H + M)
    rest = FakeRestExchange([_row(DAY + i * H, 300.0 + i) for i in range(4)])
    h = Harness(clock, rest=rest)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)
    h.kline(DAY + 4 * H, 104.0)
    await until(lambda: len(h.recorder.bars) == 4)

    got = [(b.ohlcv.timestamp, b.replay) for b in h.recorder.bars if b.ohlcv]
    assert got == [(DAY, False), (DAY + H, False), (DAY + 2 * H, True), (DAY + 3 * H, False)]
    assert h.recorder.bars[2].ohlcv is not None
    assert h.recorder.bars[2].ohlcv.close == Decimal("302.0")
    assert rest.calls[0][0] == "BTC/USDT"
    await h.stop()


# ---------------------------------------------------------------------------
# Multi-timeframe barrier
# ---------------------------------------------------------------------------


async def test_mtf_barrier_orders_higher_tf_first() -> None:
    clock = Clock(DAY + 4 * H + 2 * timedelta(seconds=1))
    h = Harness(clock)
    await h.start(timeframes=[Timeframe.H1, Timeframe.H4])
    h.kline(DAY, 100.0, tf="4h")
    h.kline(DAY + 3 * H, 100.0)
    h.kline(DAY + 4 * H, 101.0)
    await until(lambda: h.ws[0].watch_calls >= 4)
    assert h.recorder.bars == []

    h.kline(DAY + 4 * H, 101.0, tf="4h")
    await until(lambda: len(h.recorder.bars) == 2)
    assert [(b.timeframe, b.ohlcv.timestamp) for b in h.recorder.bars if b.ohlcv] == [
        (Timeframe.H4, DAY),
        (Timeframe.H1, DAY + 3 * H),
    ]
    await h.stop()


async def test_mtf_barrier_waits_for_lower_tf_too() -> None:
    clock = Clock(DAY + 4 * H + 2 * timedelta(seconds=1))
    h = Harness(clock)
    await h.start(timeframes=[Timeframe.H1, Timeframe.H4])
    h.kline(DAY + 3 * H, 100.0)
    h.kline(DAY, 100.0, tf="4h")
    h.kline(DAY + 4 * H, 101.0, tf="4h")
    await until(lambda: h.ws[0].watch_calls >= 4)
    assert h.recorder.bars == []
    h.kline(DAY + 4 * H, 101.0)
    await until(lambda: len(h.recorder.bars) == 2)
    assert [b.timeframe for b in h.recorder.bars] == [Timeframe.H4, Timeframe.H1]
    await h.stop()


async def test_mtf_barrier_timeout() -> None:
    clock = Clock(DAY + 4 * H + 2 * timedelta(seconds=1))
    h = Harness(clock, mtf_barrier_s=5.0)
    await h.start(timeframes=[Timeframe.H1, Timeframe.H4])
    h.kline(DAY, 100.0, tf="4h")
    h.kline(DAY + 3 * H, 100.0)
    h.kline(DAY + 4 * H, 101.0)
    h.kline(DAY + 4 * H, 101.5)
    h.kline(DAY + 4 * H, 102.0)
    await until(lambda: h.ws[0].watch_calls >= 6)
    await asyncio.sleep(0.03)
    assert h.recorder.bars == []

    clock.t += 5.0
    await until(lambda: len(h.recorder.bars) == 1)
    bar = h.recorder.bars[0]
    assert (bar.timeframe, bar.ohlcv.timestamp if bar.ohlcv else None) == (
        Timeframe.H1,
        DAY + 3 * H,
    )
    h.kline(DAY + 4 * H, 102.0, tf="4h")
    await until(lambda: len(h.recorder.bars) == 2)
    assert h.recorder.bars[1].timeframe == Timeframe.H4
    await h.stop()


# ---------------------------------------------------------------------------
# Warm-up
# ---------------------------------------------------------------------------


def _hourly(start: datetime, count: int, base: float = 1000.0) -> list[OHLCV]:
    out = []
    for i in range(count):
        c = Decimal(str(base + i))
        out.append(
            OHLCV(
                open=c,
                high=c + 1,
                low=c - 1,
                close=c,
                volume=Decimal(10),
                timestamp=start + i * H,
            )
        )
    return out


async def test_warm_up_fills_tail_and_rereads() -> None:
    now = DAY + 30 * 24 * H + 25 * M
    clock = Clock(now)
    end = last_closed_open(now, Timeframe.H1)
    stored_end = end - 5 * H
    repo = MemoryRepo()
    repo.seed("BTCUSDT", Timeframe.H1, _hourly(stored_end - 1199 * H, 1200))
    rest = FakeRestExchange([_row(stored_end + i * H, 5000.0 + i) for i in range(1, 7)])
    h = Harness(clock, repo=repo, rest=rest)

    warm = await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)

    bars = warm[("BTCUSDT", Timeframe.H1)]
    assert len(bars) == 1000
    assert bars[-1].timestamp == end
    assert [b.timestamp for b in bars[-6:]] == [stored_end + i * H for i in range(6)]
    assert len(repo.persisted) == 5
    assert [b.timestamp for *_, b in repo.persisted] == [stored_end + i * H for i in range(1, 6)]
    assert all(b.timestamp + H <= now for *_, b in repo.persisted)
    assert rest.calls[0][0] == "BTC/USDT"
    warmup_count = REGISTRY.get_sample_value(
        "hydra_feed_bars_total",
        {"exchange_id": "binance", "symbol": "BTCUSDT", "timeframe": "1h", "source": "warmup"},
    )
    assert warmup_count == 1000.0


async def test_warm_up_up_to_date_db_needs_no_rest() -> None:
    now = DAY + 30 * 24 * H + 25 * M
    end = last_closed_open(now, Timeframe.H1)
    repo = MemoryRepo()
    repo.seed("BTCUSDT", Timeframe.H1, _hourly(end - 1499 * H, 1500))
    h = Harness(Clock(now), repo=repo, history_window=1000)
    warm = await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=1200)
    assert len(warm[("BTCUSDT", Timeframe.H1)]) == 1200
    assert h.rest.calls == []


async def test_warm_up_insufficient_history_raises() -> None:
    now = DAY + 25 * M
    end = last_closed_open(now, Timeframe.H1)
    rest = FakeRestExchange([_row(end - i * H, 100.0) for i in range(10)])
    h = Harness(Clock(now), repo=MemoryRepo(), rest=rest)
    with pytest.raises(InsufficientHistoryError) as info:
        await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)
    assert (info.value.have, info.value.need, info.value.symbol) == (10, 50, "BTCUSDT")


async def test_warm_up_without_repository_returns_rest_bars() -> None:
    now = DAY + 2 * 24 * H + 25 * M
    end = last_closed_open(now, Timeframe.H1)
    rest = FakeRestExchange([_row(end - i * H, 100.0 + i) for i in range(30)])
    h = Harness(Clock(now), rest=rest, history_window=20)
    warm = await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=10)
    bars = warm[("BTCUSDT", Timeframe.H1)]
    assert len(bars) == 20
    assert bars[-1].timestamp == end


async def test_warm_up_seeds_live_assembler() -> None:
    now = DAY + 2 * 24 * H + 25 * M
    end = last_closed_open(now, Timeframe.H1)
    repo = MemoryRepo()
    repo.seed("BTCUSDT", Timeframe.H1, _hourly(end - 99 * H, 100))
    clock = Clock(now)
    h = Harness(clock, repo=repo, history_window=100)
    await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)
    await h.start()

    h.kline(end, 1.0)
    h.kline(end + H, 2.0)
    clock.set(end + 2 * H + M)
    h.kline(end + 2 * H, 3.0)
    await until(lambda: len(h.recorder.bars) == 1)
    assert [b.ohlcv.timestamp for b in h.recorder.bars if b.ohlcv] == [end + H]
    await h.stop()


async def test_consumer_lag_marks_stale_then_live() -> None:
    clock = Clock(DAY + H + M)
    h = Harness(clock)
    gate = asyncio.Event()

    async def slow(event: Event) -> None:
        await gate.wait()

    await h.bus.subscribe("bar", slow)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 100.0)
    await until(lambda: h.feed._feeds["binance"].inflight_since is not None)
    clock.t += 3601
    await until(lambda: h.feed.stale_reasons("binance") == {"*": ["consumer_lag"]})

    gate.set()
    await until(lambda: [s.status for s in h.recorder.statuses] == ["stale", "live"])
    assert h.recorder.statuses[0].reason == "consumer_lag"
    await h.stop()


# ---------------------------------------------------------------------------
# Gap replay, REST outages and deferred gaps
# ---------------------------------------------------------------------------


def _gated_sleep(gate: asyncio.Event) -> Callable[[float], Any]:
    async def sleep(seconds: float) -> None:
        if seconds >= ingestion._GAP_RETRY_INITIAL_S:
            await gate.wait()
        await asyncio.sleep(0)

    return sleep


class BlockingRest(FakeRestExchange):
    def __init__(self, rows: list[list[Any]]) -> None:
        super().__init__(rows)
        self.gate = asyncio.Event()
        self.blocked = asyncio.Event()

    async def fetch_ohlcv(
        self, symbol: str, timeframe: str, since: int | None = None, limit: int | None = None
    ) -> list[list[Any]]:
        self.blocked.set()
        await self.gate.wait()
        return await super().fetch_ohlcv(symbol, timeframe, since, limit)


async def test_newest_bar_filled_at_a_boundary_is_evaluated() -> None:
    now = DAY + 10 * H + 59 * M + timedelta(seconds=59)
    clock = Clock(now)
    end = last_closed_open(now, Timeframe.H1)
    repo = MemoryRepo()
    repo.seed("BTCUSDT", Timeframe.H1, _hourly(end - 99 * H, 100))
    rest = FakeRestExchange([_row(DAY + i * H, 500.0 + i) for i in range(12)])
    h = Harness(clock, repo=repo, rest=rest, history_window=100)
    await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)
    await h.start()
    await until(lambda: h.ws[0].watch_calls >= 1)

    clock.set(DAY + 11 * H + timedelta(seconds=2))
    h.kline(DAY + 11 * H, 600.0)
    await until(lambda: len(h.recorder.bars) == 1)

    bar = h.recorder.bars[0]
    assert bar.ohlcv is not None
    assert (bar.ohlcv.timestamp, bar.replay, bar.ohlcv.close) == (
        DAY + 10 * H,
        False,
        Decimal("510.0"),
    )
    await h.stop()


async def test_rest_outage_keeps_ws_bars_flowing_and_fills_the_gap_later() -> None:
    clock = Clock(DAY + H + M)
    rest = FakeRestExchange([_row(DAY + i * H, 300.0 + i) for i in range(10)])
    h = Harness(clock, rest=rest, sleep=_throttled_sleep)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    rest.errors = [ccxt.PermissionDenied("418 banned")] * 10_000
    clock.set(DAY + 3 * H + M)
    h.ws[0].feed(ConnectionError("blip"))
    await until(lambda: h.feed.stale_reasons("binance") == {"BTCUSDT/1h": ["gap_unfilled"]})
    for i in (3, 4, 5):
        clock.set(DAY + i * H + M)
        h.kline(DAY + i * H, 100.0 + i)
    await until(lambda: len(h.recorder.bars) == 3)

    got = [(b.ohlcv.timestamp, b.replay) for b in h.recorder.bars if b.ohlcv]
    assert got == [(DAY, False), (DAY + 3 * H, False), (DAY + 4 * H, False)]
    assert len(h.ws) == 1
    assert h.ws[0].watch_calls >= 5
    assert (
        REGISTRY.get_sample_value("hydra_ws_reconnects_total", {"exchange_id": "binance"}) is None
    )
    assert not h.feed.is_live("binance")

    rest.errors.clear()
    await until(lambda: len(h.recorder.bars) == 5)
    late = [(b.ohlcv.timestamp, b.replay, b.ohlcv.close) for b in h.recorder.bars[3:] if b.ohlcv]
    assert late == [(DAY + H, True, Decimal("301.0")), (DAY + 2 * H, True, Decimal("302.0"))]
    await until(lambda: h.feed.is_live("binance"))
    assert [(s.status, s.reason, s.symbol) for s in h.recorder.statuses] == [
        ("stale", "gap_unfilled", "BTCUSDT"),
        ("live", "gap_unfilled", "BTCUSDT"),
    ]
    await h.stop()


async def test_rate_limited_rest_never_stalls_the_watch_loop() -> None:
    clock = Clock(DAY + H + M)
    rest = FakeRestExchange([_row(DAY + i * H, 300.0 + i) for i in range(10)])
    gate = asyncio.Event()
    h = Harness(clock, rest=rest, sleep=_gated_sleep(gate))
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    rest.errors = [ccxt.DDoSProtection("418 I'm a teapot")] * 3
    clock.set(DAY + 3 * H + M)
    h.ws[0].feed(ConnectionError("blip"))
    await until(lambda: h.feed.stale_reasons("binance") == {"BTCUSDT/1h": ["gap_unfilled"]})
    for i in (3, 4):
        clock.set(DAY + i * H + M)
        h.kline(DAY + i * H, 100.0 + i)
    await until(lambda: len(h.recorder.bars) == 2)
    assert (h.recorder.bars[1].ohlcv.timestamp, h.recorder.bars[1].replay) == (DAY + 3 * H, False)
    assert len(rest.calls) == 1
    assert (len(h.ws), h.ws[0].close_calls) == (1, 0)
    assert (
        REGISTRY.get_sample_value("hydra_ws_reconnects_total", {"exchange_id": "binance"}) is None
    )

    rest.errors.clear()
    gate.set()
    await until(lambda: len(h.recorder.bars) == 4)
    late = [(b.ohlcv.timestamp, b.replay) for b in h.recorder.bars[2:] if b.ohlcv]
    assert late == [(DAY + H, True), (DAY + 2 * H, True)]
    await until(lambda: h.feed.is_live("binance"))
    await h.stop()


async def test_hole_fill_failure_is_retried_not_skipped() -> None:
    clock = Clock(DAY + 4 * H + M)
    repo = MemoryRepo()
    rest = FakeRestExchange([_row(DAY + i * H, 300.0 + i) for i in range(4)])
    h = Harness(clock, repo=repo, rest=rest)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    rest.errors = [ccxt.PermissionDenied("418 banned")]
    h.kline(DAY + 4 * H, 104.0)
    await until(lambda: len(h.recorder.bars) == 4)

    got = [(b.ohlcv.timestamp, b.replay) for b in h.recorder.bars if b.ohlcv]
    assert got == [(DAY, False), (DAY + H, False), (DAY + 2 * H, True), (DAY + 3 * H, False)]
    assert len(rest.calls) == 2
    await until(lambda: len(repo.persisted) == 4)
    assert sorted(b.timestamp for *_, b in repo.persisted) == [DAY + i * H for i in range(4)]
    await until(lambda: len(h.recorder.statuses) == 2)
    assert [(s.status, s.reason) for s in h.recorder.statuses] == [
        ("stale", "gap_unfilled"),
        ("live", "gap_unfilled"),
    ]
    await h.stop()


async def test_short_rest_answer_is_deferred_until_the_exchange_has_the_bar() -> None:
    clock = Clock(DAY + H + M)
    rest = FakeRestExchange([_row(DAY + i * H, 300.0 + i) for i in range(2)])
    gate = asyncio.Event()
    h = Harness(clock, rest=rest, sleep=_gated_sleep(gate))
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    clock.set(DAY + 3 * H + M)
    h.ws[0].feed(ConnectionError("blip"))
    await until(lambda: len(h.recorder.bars) == 2)
    assert (h.recorder.bars[1].ohlcv.timestamp, h.recorder.bars[1].replay) == (DAY + H, True)
    assert h.feed.stale_reasons("binance") == {"BTCUSDT/1h": ["gap_unfilled"]}

    rest.rows.append(_row(DAY + 2 * H, 302.0))
    gate.set()
    await until(lambda: len(h.recorder.bars) == 3)
    newest = h.recorder.bars[2]
    assert newest.ohlcv is not None
    assert (newest.ohlcv.timestamp, newest.replay, newest.ohlcv.close) == (
        DAY + 2 * H,
        False,
        Decimal("302.0"),
    )
    await until(lambda: h.feed.is_live("binance"))
    await h.stop()


async def test_gap_the_exchange_never_fills_is_given_up_after_the_budget() -> None:
    clock = Clock(DAY + H + M)
    rest = FakeRestExchange([_row(DAY + i * H, 300.0 + i) for i in range(2)])
    gate = asyncio.Event()
    gate.set()
    h = Harness(clock, rest=rest, sleep=_gated_sleep(gate))
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    clock.set(DAY + 3 * H + M)
    h.ws[0].feed(ConnectionError("blip"))
    await until(lambda: len(h.recorder.statuses) == 2)

    assert [(s.status, s.reason) for s in h.recorder.statuses] == [
        ("stale", "gap_unfilled"),
        ("live", "gap_unfilled"),
    ]
    assert [c[2] for c in rest.calls] == [_ms(DAY + H)] + [_ms(DAY + 2 * H)] * (
        1 + ingestion._GAP_EMPTY_ATTEMPTS
    )
    assert h.feed.is_live("binance")
    await h.stop()


async def test_watch_skips_an_instance_closed_during_resync() -> None:
    clock = Clock(DAY + H + M)
    rest = BlockingRest([_row(DAY + i * H, 300.0 + i) for i in range(5)])
    h = Harness(clock, rest=rest, ws_silence_s=10_000.0)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    clock.set(DAY + 2 * H + 2 * M)
    h.ws[0].feed(ConnectionError("blip"))
    await until(rest.blocked.is_set)
    calls = h.ws[0].watch_calls
    clock.t += 10_001
    await until(lambda: h.ws[0].close_calls == 1)
    rest.gate.set()
    await until(lambda: len(h.ws) == 2 and h.ws[1].watch_calls >= 1)

    assert h.ws[0].watch_calls == calls
    await h.stop()


# ---------------------------------------------------------------------------
# Shared gauges, warm-up holes, persistence, replay gating, full queue
# ---------------------------------------------------------------------------


async def test_interval_gauge_survives_while_another_feed_owns_the_stream() -> None:
    clock = Clock(DAY + H + M)
    a = Harness(clock)
    b = Harness(clock)
    await a.start()
    await b.start()
    await a.stop()

    b.kline(DAY, 100.0)
    b.kline(DAY + H, 101.0)
    await until(lambda: len(b.recorder.bars) == 1)
    assert _gauge("hydra_feed_bar_interval_seconds") == 3600
    assert _gauge("hydra_feed_last_closed_bar_timestamp_seconds") == DAY.timestamp()

    await b.stop()
    assert _gauge("hydra_feed_bar_interval_seconds") is None
    assert _gauge("hydra_feed_last_closed_bar_timestamp_seconds") is None


async def test_warm_up_repairs_an_interior_hole() -> None:
    now = DAY + 60 * 24 * H + 25 * M
    end = last_closed_open(now, Timeframe.H1)
    stored = _hourly(end - 1099 * H, 1100)
    first_hole = stored[1050].timestamp
    del stored[1050:1053]
    repo = MemoryRepo()
    repo.seed("BTCUSDT", Timeframe.H1, stored)
    rest = FakeRestExchange([_row(end - i * H, 7.0) for i in range(60)])
    h = Harness(Clock(now), repo=repo, rest=rest, history_window=1000)

    warm = await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)

    bars = warm[("BTCUSDT", Timeframe.H1)]
    assert [b.timestamp for b in bars] == [end - (999 - i) * H for i in range(1000)]
    assert first_hole == end - 49 * H
    assert rest.calls[0][2] == _ms(first_hole)
    assert [b.timestamp for *_, b in repo.persisted] == [first_hole + i * H for i in range(50)]


async def test_warm_up_never_fetches_more_than_the_window() -> None:
    now = DAY + 400 * 24 * H + 25 * M
    end = last_closed_open(now, Timeframe.H1)
    repo = MemoryRepo()
    repo.seed("BTCUSDT", Timeframe.H1, _hourly(end - 365 * 24 * H - 99 * H, 100))
    rest = FakeRestExchange([_row(end - i * H, 50.0) for i in range(24 * 400)])
    h = Harness(Clock(now), repo=repo, rest=rest, history_window=100)

    warm = await h.feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=50)

    bars = warm[("BTCUSDT", Timeframe.H1)]
    assert [b.timestamp for b in bars] == [end - (99 - i) * H for i in range(100)]
    assert [c[2] for c in rest.calls] == [_ms(end - 99 * H)]
    assert len(repo.persisted) == 100


async def test_persist_retries_through_an_outage_longer_than_seven_seconds() -> None:
    clock = Clock(DAY + 3 * H + M)
    repo = MemoryRepo()
    repo.fail_next = 8
    h = Harness(clock, repo=repo)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    h.kline(DAY + 2 * H, 102.0)
    await until(lambda: len(h.recorder.bars) == 2)
    await until(lambda: len(repo.persisted) == 2)

    assert [b.timestamp for *_, b in repo.persisted] == [DAY, DAY + H]
    errors = REGISTRY.get_sample_value(
        "hydra_feed_persist_errors_total", {"exchange_id": "binance"}
    )
    assert errors == 8.0

    repo.fail_next = 10**9
    h.kline(DAY + 3 * H, 103.0)
    await until(lambda: h.feed.persist_backlog("binance") == 1)
    repo.fail_next = 0
    await until(lambda: len(repo.persisted) == 3)
    assert h.feed.persist_backlog("binance") == 0
    await h.stop()


async def test_replay_bars_are_persisted_but_not_published_by_default() -> None:
    assert inspect.signature(ExchangeFeedManager).parameters["publish_replay"].default is False
    clock = Clock(DAY + H + 10 * M)
    repo = MemoryRepo()
    rest = FakeRestExchange([_row(DAY + i * H, 200.0 + i) for i in range(5)])
    h = Harness(clock, repo=repo, rest=rest, publish_replay=False)
    await h.start()
    h.kline(DAY, 100.0)
    h.kline(DAY + H, 101.0)
    await until(lambda: len(h.recorder.bars) == 1)

    clock.set(DAY + 3 * H + 10 * M)
    h.ws[0].feed(ConnectionError("blip"))
    await until(lambda: len(repo.persisted) == 3)
    await until(lambda: len(h.recorder.bars) == 2)

    got = [(b.ohlcv.timestamp, b.replay) for b in h.recorder.bars if b.ohlcv]
    assert got == [(DAY, False), (DAY + 2 * H, False)]
    assert [b.timestamp for *_, b in repo.persisted] == [DAY, DAY + H, DAY + 2 * H]
    await h.stop()


async def test_consumer_lag_survives_a_full_queue_and_recovers() -> None:
    clock = Clock(DAY + 10 * H + M)
    h = Harness(clock, queue_maxsize=2)
    gate = asyncio.Event()

    async def stuck(event: Event) -> None:
        await gate.wait()

    await h.bus.subscribe("bar", stuck)
    await h.start()
    for i in range(8):
        h.kline(DAY + i * H, 100.0 + i)
    await until(lambda: h.feed.stale_reasons("binance") == {"*": ["consumer_lag"]})
    assert h.recorder.statuses == []

    gate.set()
    await until(lambda: len(h.recorder.statuses) == 2)
    assert [(s.status, s.reason) for s in h.recorder.statuses] == [
        ("stale", "consumer_lag"),
        ("live", "consumer_lag"),
    ]
    assert [b.ohlcv.timestamp for b in h.recorder.bars if b.ohlcv] == [
        DAY + i * H for i in range(7)
    ]
    await h.stop()
