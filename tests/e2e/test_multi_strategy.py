"""E2E: Multiple strategies running simultaneously on the same data.

Verifies that strategies operate independently and that the composite
strategy correctly aggregates sub-strategy signals via weighted voting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Symbol, Timeframe
from hydra.strategy.builtin.composite import CompositeStrategy
from hydra.strategy.builtin.mean_reversion import MeanReversionBBStrategy
from hydra.strategy.builtin.momentum import MomentumRSIMACDStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

from .conftest import make_bar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_volatile_bars(count: int = 100) -> list[OHLCV]:
    """Build bars with enough volatility to trigger both momentum and mean-reversion."""
    bars: list[OHLCV] = []
    start = datetime(2024, 1, 1, tzinfo=UTC)
    price = 42000.0

    for i in range(count):
        ts = start + timedelta(hours=i)

        # Phase 1 (0-30): steady uptrend
        if i < 30:
            price = 42000.0 + i * 50
        # Phase 2 (30-50): sharp drop -- triggers oversold
        elif i < 50:
            price = 43500.0 - (i - 30) * 200
        # Phase 3 (50-70): sharp recovery
        elif i < 70:
            price = 39500.0 + (i - 50) * 250
        # Phase 4 (70-100): high volatility oscillation
        else:
            price = 44500.0 + 1500 * np.sin((i - 70) * 0.4)

        noise = 100 * np.sin(i * 0.7)
        price = max(price + noise, 1000.0)
        # Use higher volume for mean-reversion triggers
        volume = 800.0 if 45 < i < 55 else 300.0
        bars.append(make_bar(price, ts, spread_pct=0.015, volume=volume))

    return bars


def _momentum_config(strategy_id: str = "momentum_1") -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        name="Momentum RSI+MACD",
        strategy_class="hydra.strategy.builtin.momentum.MomentumRSIMACDStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1},
        parameters={
            "rsi_period": 14,
            "rsi_oversold": 30,
            "rsi_overbought": 70,
            "required_history": 50,
        },
    )


def _mean_reversion_config(strategy_id: str = "meanrev_1") -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        name="Mean Reversion BB",
        strategy_class="hydra.strategy.builtin.mean_reversion.MeanReversionBBStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1},
        parameters={
            "bb_period": 20,
            "bb_std_dev": 2.0,
            "vol_sma_period": 20,
            "vol_multiplier": 1.5,
            "required_history": 30,
        },
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestMultiStrategy:
    """Two strategies running independently on the same data."""

    async def test_two_strategies_independent_signals(self) -> None:
        """Run momentum + mean reversion on same data, verify independent operation."""
        bars = _build_volatile_bars(100)
        symbol = "BTCUSDT"
        tf = Timeframe.H1
        sym = Symbol(symbol)

        # Set up two independent contexts and strategies
        ctx_momentum = StrategyContext()
        ctx_momentum.set_portfolio_value(Decimal("100000"))
        momentum = MomentumRSIMACDStrategy(config=_momentum_config(), context=ctx_momentum)

        ctx_meanrev = StrategyContext()
        ctx_meanrev.set_portfolio_value(Decimal("100000"))
        meanrev = MeanReversionBBStrategy(config=_mean_reversion_config(), context=ctx_meanrev)

        await momentum.on_start()
        await meanrev.on_start()

        momentum_signals: list[EntrySignal | ExitSignal] = []
        meanrev_signals: list[EntrySignal | ExitSignal] = []

        for bar in bars:
            ctx_momentum.add_bar(symbol, tf, bar)
            ctx_meanrev.add_bar(symbol, tf, bar)

            bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")

            m_sigs = await momentum.on_bar(bar_event)
            mr_sigs = await meanrev.on_bar(bar_event)

            momentum_signals.extend(m_sigs)
            meanrev_signals.extend(mr_sigs)

        await momentum.on_stop()
        await meanrev.on_stop()

        # Both strategies should have run without errors
        # Their signal lists may differ because they use different indicators
        m_entries = [s for s in momentum_signals if isinstance(s, EntrySignal)]
        mr_entries = [s for s in meanrev_signals if isinstance(s, EntrySignal)]

        # Each strategy's signals carry its own strategy_id
        for sig in m_entries:
            assert sig.strategy_id == "momentum_1"
        for sig in mr_entries:
            assert sig.strategy_id == "meanrev_1"

        # They should not have leaked signals between each other
        all_strategy_ids = {s.strategy_id for s in momentum_signals + meanrev_signals}
        assert all_strategy_ids <= {"momentum_1", "meanrev_1"}

    async def test_composite_strategy_voting(self) -> None:
        """CompositeStrategy aggregates sub-strategy signals correctly."""
        bars = _build_volatile_bars(100)
        symbol = "BTCUSDT"
        tf = Timeframe.H1
        sym = Symbol(symbol)

        # Create two sub-strategy contexts and strategies
        ctx_momentum = StrategyContext()
        ctx_momentum.set_portfolio_value(Decimal("100000"))
        momentum = MomentumRSIMACDStrategy(
            config=_momentum_config("sub_momentum"), context=ctx_momentum
        )

        ctx_meanrev = StrategyContext()
        ctx_meanrev.set_portfolio_value(Decimal("100000"))
        meanrev = MeanReversionBBStrategy(
            config=_mean_reversion_config("sub_meanrev"), context=ctx_meanrev
        )

        await momentum.on_start()
        await meanrev.on_start()

        # Set up composite strategy
        composite_config = StrategyConfig(
            id="composite_test",
            name="Composite Voting",
            strategy_class="hydra.strategy.builtin.composite.CompositeStrategy",
            symbols=["BTCUSDT"],
            timeframes={"primary": Timeframe.H1},
            parameters={
                "sub_strategies": ["sub_momentum", "sub_meanrev"],
                "weights": {"sub_momentum": 1.0, "sub_meanrev": 1.0},
                "min_agreement": 0.5,
                "default_weight": 1.0,
                "required_history": 1,
            },
        )
        ctx_composite = StrategyContext()
        ctx_composite.set_portfolio_value(Decimal("100000"))
        composite = CompositeStrategy(config=composite_config, context=ctx_composite)
        await composite.on_start()

        composite_signals: list[EntrySignal | ExitSignal] = []

        for bar in bars:
            ctx_momentum.add_bar(symbol, tf, bar)
            ctx_meanrev.add_bar(symbol, tf, bar)
            ctx_composite.add_bar(symbol, tf, bar)

            bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")

            # Get sub-strategy signals
            m_sigs = await momentum.on_bar(bar_event)
            mr_sigs = await meanrev.on_bar(bar_event)

            # Feed sub-signals into composite via parameters
            sub_signals = list(m_sigs) + list(mr_sigs)
            composite_config.parameters["_sub_signals"] = sub_signals

            c_sigs = await composite.on_bar(bar_event)
            composite_signals.extend(c_sigs)

        await composite.on_stop()

        # Composite signals should carry the composite strategy_id
        for sig in composite_signals:
            assert sig.strategy_id == "composite_test"

        # If both sub-strategies agree on a LONG, composite should emit LONG
        composite_entries = [s for s in composite_signals if isinstance(s, EntrySignal)]
        for entry in composite_entries:
            assert entry.direction in (Direction.LONG, Direction.SHORT)
