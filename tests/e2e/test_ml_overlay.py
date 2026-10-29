"""E2E: the ML-native strategy consumes a real Production model's predictions.

The fake momentum heuristic is gone; ``MLEnsembleStrategy`` now reads the
Production model through the real attach pipeline (a real ONNX fixture:
P(up) = 0.01 * RSI(14) of the 1h closes, loaded by real onnxruntime), so the
entry/exit decisions are asserted against the pinned P(up) values — a strictly
rising series has RSI(14) = 100 -> P(up) = 1.0, an equal alternation has
RSI = 51.9178 -> P(up) = 0.519178 between the bands, and the unattached model
refuses loudly instead of trading silently. No exchange, DB or Redis is
needed: the model is the fixture artifact and the bars are deterministic.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Position, Symbol, Timeframe
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.serving import _reset_attached
from hydra.strategy.builtin.ml_ensemble import MLEnsembleStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.ml_access import _reset_providers
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

from .conftest import make_bar

MODEL = "e2e_alpha"
N_FEATURES = len(DEFAULT_FEATURE_SPEC.feature_names)
T0 = datetime(2024, 1, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_ml_state() -> AsyncIterator[None]:
    _reset_attached()
    _reset_providers()
    yield
    _reset_attached()
    _reset_providers()


async def _attach_fixture(models_dir: Path) -> None:
    from hydra.ml.consumers import attach_native_provider
    from hydra.ml.serving import attach_artifact

    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / "fixture.onnx").write_bytes(confidence_onnx(meta, N_FEATURES))
    attach_native_provider(await attach_artifact(models_dir, "fixture.onnx", MODEL))


def _ml_config(
    entry_threshold: float = 0.6,
    exit_threshold: float = 0.4,
    strategy_id: str = "ml_test",
) -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        name="ML Ensemble Test",
        strategy_class="hydra.strategy.builtin.ml_ensemble.MLEnsembleStrategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1, "confirmation": Timeframe.H4, "entry": Timeframe.D1},
        parameters={
            "model_name": MODEL,
            "entry_threshold": entry_threshold,
            "exit_threshold": exit_threshold,
            "required_history": 2000,
        },
    )


def _build_bars(closes: list[float]) -> list[OHLCV]:
    return [
        make_bar(close, T0 + timedelta(hours=i), volume=500.0) for i, close in enumerate(closes)
    ]


async def _run_strategy(
    strategy: MLEnsembleStrategy,
    bars: list[OHLCV],
) -> list[EntrySignal | ExitSignal]:
    context = strategy.context
    signals: list[EntrySignal | ExitSignal] = []
    sym = Symbol("BTCUSDT")
    tf = Timeframe.H1
    for bar in bars:
        context.add_bar(str(sym), tf, bar)
        bar_event = BarEvent(symbol=sym, timeframe=tf, ohlcv=bar, exchange_id="binance")
        signals.extend(await strategy.on_bar(bar_event))
    return signals


@pytest.mark.e2e
class TestMLNativeStrategy:
    """Entries and exits driven by the Production model's P(up)."""

    async def test_a_rising_market_enters_long_then_holds(self, tmp_path: Path) -> None:
        await _attach_fixture(tmp_path)
        # strictly rising closes: RSI(14) = 100 -> P(up) = 1.0 >= entry 0.6;
        # the level-triggered entry fires once, then the session's fill
        # feedback (the position) holds it like every other strategy
        bars = _build_bars([40000.0 + 100.0 * i for i in range(50)])
        strategy = MLEnsembleStrategy(_ml_config(strategy_id="ml_long"), StrategyContext())
        await strategy.on_start()
        entries: list[EntrySignal] = []
        for bar in bars:
            strategy.context.add_bar("BTCUSDT", Timeframe.H1, bar)
            sigs = await strategy.on_bar(
                BarEvent(
                    symbol=Symbol("BTCUSDT"),
                    timeframe=Timeframe.H1,
                    ohlcv=bar,
                    exchange_id="binance",
                )
            )
            entries.extend(s for s in sigs if isinstance(s, EntrySignal))
            if entries:
                # the position the session's fill handler would create
                strategy.context.set_position(
                    "BTCUSDT",
                    Position(
                        symbol=Symbol("BTCUSDT"),
                        direction=Direction.LONG,
                        quantity=Decimal("1"),
                        avg_entry_price=bar.close,
                        unrealized_pnl=Decimal("0"),
                        realized_pnl=Decimal("0"),
                        strategy_id="ml_long",
                        exchange_id="binance",
                    ),
                )
        await strategy.on_stop()

        assert len(entries) == 1
        assert entries[0].direction is Direction.LONG
        assert float(entries[0].strength) == pytest.approx(1.0, abs=1e-9)

    async def test_a_flat_market_emits_nothing_between_the_bands(self, tmp_path: Path) -> None:
        await _attach_fixture(tmp_path)
        # alternating closes: RSI(14) = 51.9178 -> P(up) = 0.519178, between
        # the SHORT band (<= 1 - 0.6) and the LONG band (>= 0.6): no entry
        bars = _build_bars([40100.0 if i % 2 == 0 else 40000.0 for i in range(50)])
        strategy = MLEnsembleStrategy(_ml_config(strategy_id="ml_flat"), StrategyContext())
        await strategy.on_start()
        signals = await _run_strategy(strategy, bars)
        await strategy.on_stop()
        assert [s for s in signals if isinstance(s, EntrySignal)] == []

    async def test_a_held_long_exits_when_the_model_turns_bearish(self, tmp_path: Path) -> None:
        await _attach_fixture(tmp_path)
        bars = _build_bars([40000.0 - 100.0 * i for i in range(50)])  # RSI 0 -> P(up) = 0.0
        strategy = MLEnsembleStrategy(_ml_config(strategy_id="ml_exit"), StrategyContext())
        await strategy.on_start()
        strategy.context.set_position(
            "BTCUSDT",
            Position(
                symbol=Symbol("BTCUSDT"),
                direction=Direction.LONG,
                quantity=Decimal("1"),
                avg_entry_price=Decimal("40000"),
                unrealized_pnl=Decimal("0"),
                realized_pnl=Decimal("0"),
                strategy_id="ml_exit",
                exchange_id="binance",
            ),
        )
        signals = await _run_strategy(strategy, bars)
        await strategy.on_stop()
        exits = [s for s in signals if isinstance(s, ExitSignal)]
        assert len(exits) >= 1
        assert exits[0].reason == "ml_native_exit"

    async def test_a_strategy_without_its_model_refuses_loudly(self) -> None:
        # nothing attached in this process: the strategy still constructs, but
        # start (and every bar) refuses — never a silent no-signal strategy
        from hydra.strategy.ml_access import MlModelNotAttachedError

        strategy = MLEnsembleStrategy(_ml_config(strategy_id="ml_ghost"), StrategyContext())
        with pytest.raises(MlModelNotAttachedError, match="attached"):
            await strategy.on_start()
        with pytest.raises(RuntimeError, match="no attached model"):
            await strategy.on_bar(
                BarEvent(
                    symbol=Symbol("BTCUSDT"),
                    timeframe=Timeframe.H1,
                    ohlcv=make_bar(40000.0, T0, volume=500.0),
                    exchange_id="binance",
                )
            )

    async def test_a_config_without_a_model_name_is_refused_at_construction(self) -> None:
        config = _ml_config(strategy_id="ml_no_name")
        config.parameters["model_name"] = ""
        with pytest.raises(ValueError, match=r"parameters\.model_name"):
            MLEnsembleStrategy(config, StrategyContext())
