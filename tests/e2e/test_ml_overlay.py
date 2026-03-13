"""E2E: ML confidence overlay -- signal filtering and model fallback.

Tests the interaction between strategy signals and ML confidence scoring.
Uses the MLEnsembleStrategy which has a confidence threshold that filters
weak predictions.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import numpy as np
import pytest

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Symbol, Timeframe
from hydra.strategy.builtin.ml_ensemble import MLEnsembleStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

from .conftest import make_bar

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ml_config(
    confidence_threshold: float = 0.6,
    strategy_id: str = "ml_test",
) -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        name="ML Ensemble Test",
        strategy_class="hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1},
        parameters={
            "confidence_threshold": confidence_threshold,
            "required_history": 30,
        },
    )


def _build_trending_bars(count: int = 50, direction: str = "up") -> list[OHLCV]:
    """Build bars with a steep trend to drive ML placeholder confidence above threshold.

    The ML placeholder computes confidence = min(abs(short_ret) * 10, 1.0) where
    short_ret = close[-1]/close[-5] - 1.  For confidence > 0.3 we need
    abs(short_ret) > 0.03, i.e. a 3% move over 5 bars.  We use 2% per bar
    (exponential growth) to ensure this.
    """
    bars: list[OHLCV] = []
    start = datetime(2024, 1, 1, tzinfo=UTC)
    base_price = 40000.0

    for i in range(count):
        ts = start + timedelta(hours=i)
        price = base_price * (1.02**i) if direction == "up" else base_price * (0.98**i)

        bars.append(make_bar(max(price, 1000.0), ts, volume=500.0))

    return bars


def _build_flat_bars(count: int = 50) -> list[OHLCV]:
    """Build flat bars with no clear trend -- ML should have low confidence."""
    bars: list[OHLCV] = []
    start = datetime(2024, 1, 1, tzinfo=UTC)

    for i in range(count):
        ts = start + timedelta(hours=i)
        # Oscillate around 40000 with tiny amplitude
        price = 40000.0 + 5 * np.sin(i * 0.5)
        bars.append(make_bar(price, ts, volume=500.0))

    return bars


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestMLOverlay:
    """Strategy with ML confidence scoring overlay."""

    async def test_ml_confidence_filters_signals(self) -> None:
        """Low confidence prediction causes no signal to be emitted."""
        # Use flat bars where ML placeholder yields low confidence
        bars = _build_flat_bars(50)
        config = _ml_config(confidence_threshold=0.6, strategy_id="ml_filter")
        context = StrategyContext()
        context.set_portfolio_value(Decimal("100000"))
        strategy = MLEnsembleStrategy(config=config, context=context)
        await strategy.on_start()

        signals: list[EntrySignal | ExitSignal] = []
        symbol = "BTCUSDT"
        sym = Symbol(symbol)
        tf = Timeframe.H1

        for bar in bars:
            context.add_bar(symbol, tf, bar)
            bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")
            sigs = await strategy.on_bar(bar_event)
            signals.extend(sigs)

        await strategy.on_stop()

        # With flat data the ML placeholder should produce low confidence,
        # so no signals should pass the threshold
        entry_signals = [s for s in signals if isinstance(s, EntrySignal)]
        # The flat data has near-zero returns => direction=0, confidence=0
        assert len(entry_signals) == 0

    async def test_ml_high_confidence_passes_through(self) -> None:
        """High confidence predictions produce signals."""
        # Use trending bars where the ML placeholder yields high confidence
        bars = _build_trending_bars(50, direction="up")
        config = _ml_config(confidence_threshold=0.3, strategy_id="ml_pass")
        context = StrategyContext()
        context.set_portfolio_value(Decimal("100000"))
        strategy = MLEnsembleStrategy(config=config, context=context)
        await strategy.on_start()

        signals: list[EntrySignal | ExitSignal] = []
        symbol = "BTCUSDT"
        sym = Symbol(symbol)
        tf = Timeframe.H1

        for bar in bars:
            context.add_bar(symbol, tf, bar)
            bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")
            sigs = await strategy.on_bar(bar_event)
            signals.extend(sigs)

        await strategy.on_stop()

        # With trending data the ML placeholder should produce some signals
        entry_signals = [s for s in signals if isinstance(s, EntrySignal)]
        # At least some signals should pass through with a strong trend
        assert len(entry_signals) > 0

        # All signals should be LONG for uptrending data
        for sig in entry_signals:
            assert sig.direction == Direction.LONG

    async def test_ml_model_fallback(self) -> None:
        """When ML _compute_prediction returns direction=0, strategy produces no signal."""
        bars = _build_trending_bars(50, direction="up")
        config = _ml_config(confidence_threshold=0.5, strategy_id="ml_fallback")
        context = StrategyContext()
        context.set_portfolio_value(Decimal("100000"))
        strategy = MLEnsembleStrategy(config=config, context=context)
        await strategy.on_start()

        # Patch the ML prediction to simulate model unavailability
        def _fake_prediction(close, high, low, volume):
            return {"direction": 0, "confidence": 0.0}

        signals: list[EntrySignal | ExitSignal] = []
        symbol = "BTCUSDT"
        sym = Symbol(symbol)
        tf = Timeframe.H1

        with patch.object(strategy, "_compute_prediction", side_effect=_fake_prediction):
            for bar in bars:
                context.add_bar(symbol, tf, bar)
                bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")
                sigs = await strategy.on_bar(bar_event)
                signals.extend(sigs)

        await strategy.on_stop()

        # With fallback returning direction=0, no signals should be emitted
        entry_signals = [s for s in signals if isinstance(s, EntrySignal)]
        assert len(entry_signals) == 0

    async def test_ml_threshold_boundary(self) -> None:
        """Signals at exactly the confidence threshold are filtered out."""
        bars = _build_trending_bars(50, direction="up")
        # Set threshold to exactly 1.0 so nothing passes
        config = _ml_config(confidence_threshold=1.0, strategy_id="ml_boundary")
        context = StrategyContext()
        context.set_portfolio_value(Decimal("100000"))
        strategy = MLEnsembleStrategy(config=config, context=context)
        await strategy.on_start()

        signals: list[EntrySignal | ExitSignal] = []
        symbol = "BTCUSDT"
        sym = Symbol(symbol)
        tf = Timeframe.H1

        for bar in bars:
            context.add_bar(symbol, tf, bar)
            bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")
            sigs = await strategy.on_bar(bar_event)
            signals.extend(sigs)

        await strategy.on_stop()

        # With threshold=1.0 nothing should pass (confidence is always < 1.0)
        entry_signals = [s for s in signals if isinstance(s, EntrySignal)]
        assert len(entry_signals) == 0
