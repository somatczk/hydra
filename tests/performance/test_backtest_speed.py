"""Performance: Backtest execution speed.

Verifies that BacktestRunner can process large datasets within acceptable
time limits, ensuring the system is production-ready for research workflows.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.backtest.runner import BacktestRunner
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.builtin.momentum import MomentumRSIMACDStrategy
from hydra.strategy.config import StrategyConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_large_dataset(n_bars: int) -> list[OHLCV]:
    """Generate a large dataset of OHLCV bars for performance testing.

    Uses numpy for fast generation of realistic-looking price data with
    a random walk and volume variation.
    """
    rng = np.random.default_rng(42)
    # Random walk for close prices
    returns = rng.normal(loc=0.0001, scale=0.01, size=n_bars)
    close_arr = 40000.0 * np.exp(np.cumsum(returns))

    # Generate OHLCV data
    start = datetime(2023, 1, 1, tzinfo=UTC)
    bars: list[OHLCV] = []

    for i in range(n_bars):
        c = close_arr[i]
        spread = c * 0.005
        bars.append(
            OHLCV(
                open=Decimal(str(round(c + rng.normal(0, spread * 0.1), 2))),
                high=Decimal(str(round(c + abs(rng.normal(0, spread)), 2))),
                low=Decimal(str(round(c - abs(rng.normal(0, spread)), 2))),
                close=Decimal(str(round(c, 2))),
                volume=Decimal(str(round(abs(rng.normal(500, 200)), 2))),
                timestamp=start + timedelta(minutes=i),
            )
        )

    return bars


def _momentum_config() -> StrategyConfig:
    return StrategyConfig(
        id="perf_momentum",
        name="Performance Momentum",
        strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.M1},
        parameters={
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "required_history": 50,
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.performance
def test_10k_bar_backtest_under_30s() -> None:
    """Backtest of 10,000 1m bars should complete in <30s."""
    import asyncio

    bars = _generate_large_dataset(10_000)
    config = _momentum_config()
    runner = BacktestRunner()

    async def _run():
        return await runner.run(
            strategy_class=MomentumRSIMACDStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
        )

    start = time.perf_counter()
    result = asyncio.get_event_loop().run_until_complete(_run())
    elapsed = time.perf_counter() - start

    assert result.total_trades >= 0
    assert len(result.equity_curve) == len(bars) + 1
    assert elapsed < 30, f"10k bar backtest took {elapsed:.1f}s (limit: 30s)"


@pytest.mark.performance
def test_50k_bar_backtest_under_120s() -> None:
    """Backtest of 50,000 1m bars should complete in <120s."""
    import asyncio

    bars = _generate_large_dataset(50_000)
    config = _momentum_config()
    runner = BacktestRunner()

    async def _run():
        return await runner.run(
            strategy_class=MomentumRSIMACDStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
        )

    start = time.perf_counter()
    result = asyncio.get_event_loop().run_until_complete(_run())
    elapsed = time.perf_counter() - start

    assert result.total_trades >= 0
    assert len(result.equity_curve) == len(bars) + 1
    assert elapsed < 120, f"50k bar backtest took {elapsed:.1f}s (limit: 120s)"
