"""E2E: the live feed path gives the strategy the same bars, indicators and signals as a backtest.

Path A runs BacktestRunner over 600 stored 1h bars. Path B warms a StrategyEngine from the
feed's ``warm_up`` (a repository holding the first 400 bars), then streams the last 200 through
the real ccxt.pro Binance kline parser with three forming updates per candle. Entry signals
depend only on indicators (position feedback is out of scope), so both paths must agree
exactly on every bar from 400 to 599.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.backtest.runner import BacktestRunner
from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, Event, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.data.ingestion import ExchangeFeedManager
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine
from tests.fixtures.binance_kline import BinanceKlineDriver, FakeRestExchange, FakeWsExchange

START = datetime(2026, 1, 5, tzinfo=UTC)
H = timedelta(hours=1)
TOTAL = 600
WARM = 400


@dataclass(frozen=True)
class Observation:
    timestamp: datetime
    rsi: float
    ema: float
    signal: bool


RECORDS: dict[str, list[Observation]] = {}


class IndicatorParityStrategy(BaseStrategy):
    """Long entry when RSI(14) crosses above 50 while the close is within 1% of EMA(50)."""

    @property
    def required_history(self) -> int:
        return 60

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        assert bar.ohlcv is not None
        symbol, tf = str(bar.symbol), bar.timeframe
        if len(self.context.bars(symbol, tf, self.required_history)) < self.required_history:
            return []
        rsi = self.context.indicator("rsi", symbol, tf, period=14)
        ema = self.context.indicator("ema", symbol, tf, period=50)
        crossed = rsi[-2] < 50 <= rsi[-1] and float(bar.ohlcv.close) > 0.99 * ema[-1]
        RECORDS[self.config.parameters["run"]].append(
            Observation(bar.ohlcv.timestamp, float(rsi[-1]), float(ema[-1]), bool(crossed))
        )
        if not crossed:
            return []
        return [
            EntrySignal(
                symbol=bar.symbol,
                direction=Direction.LONG,
                strength=Decimal("0.5"),
                strategy_id=self.strategy_id,
                exchange_id=bar.exchange_id,
            )
        ]


def _config(run: str) -> StrategyConfig:
    return StrategyConfig(
        id=f"parity-{run}",
        name="Indicator parity",
        strategy_class=f"{__name__}.IndicatorParityStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1},
        parameters={"run": run},
    )


def _series() -> list[OHLCV]:
    bars: list[OHLCV] = []
    for i in range(TOTAL):
        mid = 40_000 + 1_500 * math.sin(i / 9) + 600 * math.sin(i / 3.7) + 4 * i
        o = round(mid + 35 * math.sin(i * 1.3), 2)
        c = round(mid + 35 * math.cos(i * 0.7), 2)
        hi = round(max(o, c) + 20 + 10 * abs(math.sin(i)), 2)
        lo = round(min(o, c) - 20 - 10 * abs(math.cos(i)), 2)
        vol = round(50 + 20 * abs(math.sin(i / 5)), 3)
        bars.append(
            OHLCV(
                open=Decimal(str(o)),
                high=Decimal(str(hi)),
                low=Decimal(str(lo)),
                close=Decimal(str(c)),
                volume=Decimal(str(vol)),
                timestamp=START + i * H,
            )
        )
    return bars


class _Repo:
    def __init__(self, bars: list[OHLCV]) -> None:
        self.bars = list(bars)

    async def get_latest_bars(
        self, exchange_id: str, symbol: str, tf: Timeframe, limit: int, end: datetime | None = None
    ) -> list[OHLCV]:
        return [b for b in self.bars if end is None or b.timestamp <= end][-limit:]

    async def upsert_closed_bars(
        self, rows: list[tuple[str, str, Timeframe, OHLCV]]
    ) -> tuple[int, int]:
        known = {b.timestamp for b in self.bars}
        self.bars.extend(b for *_, b in rows if b.timestamp not in known)
        self.bars.sort(key=lambda b: b.timestamp)
        return (len(rows), 0)


async def _until(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not cond():
        assert loop.time() < deadline, "condition not met in time"
        await asyncio.sleep(0.001)


def _forming(bar: OHLCV) -> list[tuple[float, float, float, float, float]]:
    """Three pushes of one candle; the last carries the final values."""
    o, h, low, c, v = (float(x) for x in (bar.open, bar.high, bar.low, bar.close, bar.volume))
    first_close = round((o + c) / 2, 2)
    return [
        (o, max(o, first_close), min(o, first_close), first_close, round(v / 3, 3)),
        (o, h, min(o, first_close, c), round((first_close + h) / 2, 2), round(v / 2, 3)),
        (o, h, low, c, v),
    ]


async def _run_backtest(bars: list[OHLCV]) -> list[Observation]:
    RECORDS["backtest"] = []
    await BacktestRunner().run(
        strategy_class=IndicatorParityStrategy,
        strategy_config=_config("backtest"),
        bars=bars,
        initial_capital=Decimal("100000"),
    )
    return RECORDS["backtest"]


async def _run_live(bars: list[OHLCV]) -> tuple[list[Observation], StrategyContext]:
    RECORDS["live"] = []
    clock_t = [(START + WARM * H + 30 * timedelta(seconds=1)).timestamp()]
    ws = FakeWsExchange()
    bus = InMemoryEventBus()
    feed = ExchangeFeedManager(
        event_bus=bus,
        exchange_factories={"binance": lambda: ws},
        repository=_Repo(bars[:WARM]),
        rest_factory=lambda _eid: FakeRestExchange(),
        history_window=WARM,
        watchdog_interval_s=3600,
        clock=lambda: clock_t[0],
    )
    context = StrategyContext(max_bars=1000)
    engine = StrategyEngine(config=HydraConfig(), event_bus=bus, context=context)
    cfg = _config("live")
    await engine.load_strategy_from_config(cfg, cls=IndicatorParityStrategy)

    warm = await feed.warm_up(cfg.symbols, cfg.all_timeframes(), min_bars=60)
    assert [b.timestamp for b in warm[("BTCUSDT", Timeframe.H1)]] == [
        b.timestamp for b in bars[:WARM]
    ]
    await engine.warm_up(warm)
    await engine.start()
    await feed.connect("binance", cfg.symbols, cfg.all_timeframes())

    driver = BinanceKlineDriver()
    delivered: list[datetime] = []

    async def count(event: Event) -> None:
        assert isinstance(event, BarEvent) and event.ohlcv is not None
        delivered.append(event.ohlcv.timestamp)

    await bus.subscribe("bar", count)
    for i in range(WARM, TOTAL + 1):
        open_ms = int((START + i * H).timestamp() * 1000)
        clock_t[0] = (START + i * H + 2 * timedelta(seconds=1)).timestamp()
        pushes = _forming(bars[i]) if i < TOTAL else [(1.0, 1.0, 1.0, 1.0, 1.0)]
        for ohlcv in pushes:
            ws.feed(driver.push_and_read("BTCUSDT", "BTC/USDT", "1h", open_ms, ohlcv))
        await _until(lambda i=i: len(delivered) == i - WARM)
    await feed.disconnect_all()
    await engine.stop()
    return RECORDS["live"], context


@pytest.mark.e2e
async def test_live_feed_signals_and_indicators_match_backtest() -> None:
    bars = _series()
    backtest = await _run_backtest(bars)
    live, context = await _run_live(bars)

    tail = [o for o in backtest if o.timestamp >= START + WARM * H]
    assert [o.timestamp for o in live] == [b.timestamp for b in bars[WARM:]]
    assert [o.timestamp for o in tail] == [o.timestamp for o in live]

    backtest_signals = [o.timestamp for o in tail if o.signal]
    live_signals = [o.timestamp for o in live if o.signal]
    assert backtest_signals, "the series must produce entry signals in the compared window"
    assert live_signals == backtest_signals

    np.testing.assert_allclose([o.rsi for o in live], [o.rsi for o in tail], rtol=0, atol=1e-9)
    np.testing.assert_allclose([o.ema for o in live], [o.ema for o in tail], rtol=0, atol=1e-9)

    assert context.bars("BTCUSDT", Timeframe.H1, 10_000) == bars
