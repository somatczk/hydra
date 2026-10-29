"""The ML-native strategy: deterministic signals from a real fixture model.

The model is a real ONNX artifact (``confidence_onnx``: P(up) = 0.01 *
RSI(14) of the last 1h bar), attached through the real serving pipeline and
the real provider registry, so the entry/exit decisions at the thresholds are
asserted against hand-pinned P(up) values: a strictly rising series has
RSI = 100 → P(up) = 1.0, a strictly falling one RSI = 0 → P(up) = 0.0, and an
equal up/down alternation has RSI = 50 → P(up) = 0.5.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.core.events import BarEvent, ExitSignal
from hydra.core.types import OHLCV, Direction, Symbol, Timeframe
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.serving import _reset_attached
from hydra.strategy.builtin.ml_ensemble import (
    STRATEGY_CLASS,
    MLEnsembleStrategy,
)
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.ml_access import MlModelNotAttachedError, _reset_providers
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
MODEL = "alpha"
N_FEATURES = len(DEFAULT_FEATURE_SPEC.feature_names)


@pytest.fixture(autouse=True)
def _isolate_ml_state() -> Any:
    _reset_attached()
    _reset_providers()
    yield
    _reset_attached()
    _reset_providers()


def _bar(i: int, close: float) -> OHLCV:
    value = Decimal(str(close))
    return OHLCV(
        open=value,
        high=value,
        low=value,
        close=value,
        volume=Decimal("10"),
        timestamp=T0 + i * H,
    )


def _series(closes: list[float]) -> list[OHLCV]:
    return [_bar(i, c) for i, c in enumerate(closes)]


async def _attach(tmp_path: Any) -> None:
    from hydra.ml.consumers import attach_native_provider
    from hydra.ml.serving import attach_artifact

    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    (tmp_path / "fixture.onnx").write_bytes(confidence_onnx(meta, N_FEATURES))
    attached = await attach_artifact(tmp_path, "fixture.onnx", MODEL)
    attach_native_provider(attached)


def _cfg(**parameters: Any) -> StrategyConfig:
    return StrategyConfig(
        id="native",
        name="native",
        strategy_class=STRATEGY_CLASS,
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h", "confirmation": "4h", "entry": "1d"},
        parameters={"model_name": MODEL, **parameters},
    )


def _strategy_with_bars(cfg: StrategyConfig, closes: list[float]) -> MLEnsembleStrategy:
    context = StrategyContext()
    for bar in _series(closes):
        context.add_bar("BTCUSDT", Timeframe.H1, bar)
    return MLEnsembleStrategy(cfg, context)


def _last_bar_event(closes: list[float]) -> BarEvent:
    bars = _series(closes)
    return BarEvent(
        symbol=Symbol("BTCUSDT"),
        timeframe=Timeframe.H1,
        ohlcv=bars[-1],
        exchange_id="binance",
    )


class TestConfigChecks:
    def test_a_model_name_is_required(self) -> None:
        with pytest.raises(ValueError, match=r"parameters\.model_name"):
            MLEnsembleStrategy(_cfg(model_name=""), StrategyContext())

    def test_entry_threshold_must_be_in_the_open_band(self) -> None:
        with pytest.raises(ValueError, match="entry_threshold"):
            MLEnsembleStrategy(_cfg(entry_threshold=0.5), StrategyContext())
        with pytest.raises(ValueError, match="entry_threshold"):
            MLEnsembleStrategy(_cfg(entry_threshold=1.0), StrategyContext())

    def test_exit_threshold_must_stay_below_the_entry_band(self) -> None:
        with pytest.raises(ValueError, match="exit_threshold"):
            MLEnsembleStrategy(_cfg(exit_threshold=0.7), StrategyContext())

    def test_the_frozen_grid_is_required(self) -> None:
        cfg = _cfg()
        cfg.timeframes.primary = Timeframe.M15
        with pytest.raises(ValueError, match="1h/4h/1d"):
            MLEnsembleStrategy(cfg, StrategyContext())

    def test_required_history_defaults_to_the_spec_window(self) -> None:
        strategy = MLEnsembleStrategy(_cfg(), StrategyContext())
        assert strategy.required_history == 2000


class TestOnStart:
    async def test_start_refuses_without_an_attached_model(self) -> None:
        strategy = MLEnsembleStrategy(_cfg(), StrategyContext())
        with pytest.raises(MlModelNotAttachedError, match="attached"):
            await strategy.on_start()

    async def test_start_pins_the_attached_predictor(self, tmp_path: Any) -> None:
        await _attach(tmp_path)
        strategy = MLEnsembleStrategy(_cfg(), StrategyContext())
        await strategy.on_start()
        assert strategy._predictor is not None


class TestSignals:
    """Deterministic entries and exits at the thresholds, from the fixture model."""

    async def test_high_p_up_enters_long_at_the_threshold(self, tmp_path: Any) -> None:
        await _attach(tmp_path)
        # rising closes: RSI(14) = 100 -> P(up) = 1.0 (pinned); entry_threshold 0.6
        closes = [100.0 + i for i in range(60)]
        strategy = _strategy_with_bars(_cfg(entry_threshold=0.6), closes)
        await strategy.on_start()
        signals = await strategy.on_bar(_last_bar_event(closes))
        assert len(signals) == 1
        entry = signals[0]
        assert entry.direction is Direction.LONG
        assert float(entry.strength) == pytest.approx(1.0, abs=1e-9)

    async def test_low_p_up_enters_short(self, tmp_path: Any) -> None:
        await _attach(tmp_path)
        # falling closes: RSI = 0 -> P(up) = 0.0 <= 1 - 0.6 -> a SHORT entry
        closes = [160.0 - i for i in range(60)]
        strategy = _strategy_with_bars(_cfg(entry_threshold=0.6), closes)
        await strategy.on_start()
        signals = await strategy.on_bar(_last_bar_event(closes))
        assert len(signals) == 1
        assert signals[0].direction is Direction.SHORT
        assert float(signals[0].strength) == pytest.approx(1.0, abs=1e-9)

    async def test_a_band_middle_p_up_emits_nothing(self, tmp_path: Any) -> None:
        await _attach(tmp_path)
        # equal up/down alternation: RSI = 50 -> P(up) = 0.5, between the bands
        closes = [100.0 + (i % 2) for i in range(60)]
        strategy = _strategy_with_bars(_cfg(entry_threshold=0.6), closes)
        await strategy.on_start()
        assert await strategy.on_bar(_last_bar_event(closes)) == []

    async def test_a_held_long_exits_when_p_up_falls_to_the_exit_threshold(
        self, tmp_path: Any
    ) -> None:
        await _attach(tmp_path)
        from hydra.core.types import Position

        closes = [160.0 - i for i in range(60)]  # P(up) = 0.0 <= exit_threshold 0.4
        strategy = _strategy_with_bars(_cfg(entry_threshold=0.6, exit_threshold=0.4), closes)
        await strategy.on_start()
        strategy._context.set_position(
            "BTCUSDT",
            Position(
                symbol=Symbol("BTCUSDT"),
                direction=Direction.LONG,
                quantity=Decimal("1"),
                avg_entry_price=Decimal("160"),
                unrealized_pnl=Decimal("0"),
                realized_pnl=Decimal("0"),
                strategy_id="native",
                exchange_id="binance",
            ),
        )
        signals = await strategy.on_bar(_last_bar_event(closes))
        assert len(signals) == 1
        assert isinstance(signals[0], ExitSignal)
        assert signals[0].reason == "ml_native_exit"
        assert signals[0].close_pct == 1.0

    async def test_a_held_short_exits_when_p_up_rises(self, tmp_path: Any) -> None:
        await _attach(tmp_path)
        from hydra.core.types import Position

        closes = [100.0 + i for i in range(60)]  # P(up) = 1.0 >= 1 - 0.4
        strategy = _strategy_with_bars(_cfg(entry_threshold=0.6, exit_threshold=0.4), closes)
        await strategy.on_start()
        strategy._context.set_position(
            "BTCUSDT",
            Position(
                symbol=Symbol("BTCUSDT"),
                direction=Direction.SHORT,
                quantity=Decimal("1"),
                avg_entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                realized_pnl=Decimal("0"),
                strategy_id="native",
                exchange_id="binance",
            ),
        )
        signals = await strategy.on_bar(_last_bar_event(closes))
        assert len(signals) == 1
        assert isinstance(signals[0], ExitSignal)

    async def test_a_held_long_holds_through_the_middle_band(self, tmp_path: Any) -> None:
        await _attach(tmp_path)
        from hydra.core.types import Position

        closes = [100.0 + (i % 2) for i in range(60)]  # P(up) = 0.5 > exit 0.4
        strategy = _strategy_with_bars(_cfg(entry_threshold=0.6, exit_threshold=0.4), closes)
        await strategy.on_start()
        strategy._context.set_position(
            "BTCUSDT",
            Position(
                symbol=Symbol("BTCUSDT"),
                direction=Direction.LONG,
                quantity=Decimal("1"),
                avg_entry_price=Decimal("100"),
                unrealized_pnl=Decimal("0"),
                realized_pnl=Decimal("0"),
                strategy_id="native",
                exchange_id="binance",
            ),
        )
        assert await strategy.on_bar(_last_bar_event(closes)) == []

    async def test_on_bar_without_a_pinned_predictor_raises(self, tmp_path: Any) -> None:
        # a strategy that never started (no on_start) refuses loudly, not silently
        closes = [100.0 + i for i in range(60)]
        strategy = _strategy_with_bars(_cfg(), closes)
        with pytest.raises(RuntimeError, match="no attached model"):
            await strategy.on_bar(_last_bar_event(closes))
