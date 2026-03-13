"""Performance: Event bus throughput and indicator computation speed.

Benchmarks InMemoryEventBus event throughput and indicator computation
latency to verify they meet production requirements.
"""

from __future__ import annotations

import asyncio
import time

import numpy as np
import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import EntrySignal, Event
from hydra.core.types import Direction, MarketType, Symbol
from hydra.indicators.library import atr, bollinger_bands, ema, macd, rsi, sma

# ---------------------------------------------------------------------------
# Event bus throughput
# ---------------------------------------------------------------------------


@pytest.mark.performance
def test_inmemory_bus_10k_events_per_second() -> None:
    """InMemoryEventBus should handle 10k events/sec."""
    bus = InMemoryEventBus()
    count = 0

    async def _counter(event: Event) -> None:
        nonlocal count
        count += 1

    async def _run() -> float:
        nonlocal count
        await bus.subscribe("entry_signal", _counter)

        signal = EntrySignal(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            strength="0.5",
            strategy_id="perf_test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )

        n_events = 10_000
        start = time.perf_counter()
        for _ in range(n_events):
            await bus.publish(signal)
        elapsed = time.perf_counter() - start
        return elapsed

    elapsed = asyncio.get_event_loop().run_until_complete(_run())
    events_per_sec = 10_000 / elapsed

    assert count == 10_000, f"Expected 10000 events delivered, got {count}"
    assert events_per_sec > 10_000, (
        f"Expected >10k events/sec, got {events_per_sec:.0f} events/sec "
        f"({elapsed:.3f}s for 10k events)"
    )


# ---------------------------------------------------------------------------
# Indicator computation speed
# ---------------------------------------------------------------------------


@pytest.mark.performance
def test_rsi_10k_bars_speed() -> None:
    """RSI on 10k bars should complete in <50ms."""
    data = np.random.default_rng(42).normal(loc=100, scale=5, size=10_000).cumsum()
    data = np.abs(data) + 1  # ensure positive

    start = time.perf_counter()
    result = rsi(data, 14)
    elapsed = time.perf_counter() - start

    assert len(result) == 10_000
    assert elapsed < 0.05, f"RSI on 10k bars took {elapsed:.4f}s (limit: 0.05s)"


@pytest.mark.performance
def test_sma_10k_bars_speed() -> None:
    """SMA on 10k bars should complete in <20ms."""
    data = np.random.default_rng(42).normal(loc=100, scale=5, size=10_000).cumsum()

    start = time.perf_counter()
    result = sma(data, 20)
    elapsed = time.perf_counter() - start

    assert len(result) == 10_000
    assert elapsed < 0.02, f"SMA on 10k bars took {elapsed:.4f}s (limit: 0.02s)"


@pytest.mark.performance
def test_macd_10k_bars_speed() -> None:
    """MACD on 10k bars should complete in <100ms."""
    data = np.random.default_rng(42).normal(loc=100, scale=5, size=10_000).cumsum()
    data = np.abs(data) + 1

    start = time.perf_counter()
    m_line, _s_line, _histogram = macd(data)
    elapsed = time.perf_counter() - start

    assert len(m_line) == 10_000
    assert elapsed < 0.1, f"MACD on 10k bars took {elapsed:.4f}s (limit: 0.1s)"


@pytest.mark.performance
def test_bollinger_bands_10k_speed() -> None:
    """Bollinger Bands on 10k bars should complete in <100ms."""
    data = np.random.default_rng(42).normal(loc=100, scale=5, size=10_000).cumsum()
    data = np.abs(data) + 1

    start = time.perf_counter()
    upper, _middle, _lower = bollinger_bands(data, 20, 2.0)
    elapsed = time.perf_counter() - start

    assert len(upper) == 10_000
    assert elapsed < 0.1, f"Bollinger Bands on 10k bars took {elapsed:.4f}s (limit: 0.1s)"


@pytest.mark.performance
def test_multiple_indicators_pipeline_speed() -> None:
    """Running RSI + MACD + BB + ATR sequentially on 10k bars should complete in <300ms."""
    rng = np.random.default_rng(42)
    close = np.abs(rng.normal(loc=100, scale=5, size=10_000).cumsum()) + 1
    high = close * 1.01
    low = close * 0.99

    start = time.perf_counter()
    _ = rsi(close, 14)
    _ = macd(close)
    _ = bollinger_bands(close, 20, 2.0)
    _ = atr(high, low, close, 14)
    _ = ema(close, 20)
    elapsed = time.perf_counter() - start

    assert elapsed < 0.3, f"Indicator pipeline on 10k bars took {elapsed:.4f}s (limit: 0.3s)"
