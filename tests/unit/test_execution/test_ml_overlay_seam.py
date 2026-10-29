"""The veto seam end to end: a real ``SessionRuntime`` gates real entries.

``BacktestRunner`` builds the same runtime a paper session runs, replays
closed bars onto its bus and fills real orders, so this is the P5-C seam
(``SessionRuntime._on_signal`` → the overlay) exercised whole: a fixture
Production model attached in-process decides, and the veto/pass/shadow/error
behaviour is asserted on real fills — a vetoed entry never becomes a trade.
The fixture models are real ONNX artifacts (``confidence_onnx``:
P(up) = scale * RSI(14) on the 1h closes; a strictly rising series has RSI =
100, so the permissive model answers P(up) = 1.0 and the strict one 0.1).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hydra.backtest.runner import BacktestRunner
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.ml.features import DEFAULT_FEATURE_SPEC
from hydra.ml.serving import _reset_attached, attach_artifact
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import MLOverlayConfig, StrategyConfig
from tests.fixtures.ml_onnx import confidence_onnx, frozen_spec_metadata

T0 = datetime(2026, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)
WARM = 20
TRADED = 30
N_FEATURES = len(DEFAULT_FEATURE_SPEC.feature_names)


@pytest.fixture(autouse=True)
def _isolate_ml_state() -> Any:
    _reset_attached()
    from hydra.ml.consumers import _reset_overlays

    _reset_overlays()
    yield
    _reset_attached()
    _reset_overlays()


class RisingEntry(BaseStrategy):
    """Enters LONG on the first rising close while flat (level-triggered)."""

    @property
    def required_history(self) -> int:
        return 2

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        last, prev = self.context.bars(symbol, bar.timeframe, 2)[::-1]
        if self.context.position(symbol) is not None:
            return []
        if last.close <= prev.close:
            return []
        return [
            EntrySignal(
                symbol=bar.symbol,
                direction=Direction.LONG,
                strength=Decimal("1"),
                strategy_id=self.strategy_id,
                exchange_id=self.config.exchange.exchange_id,
                market_type=self.config.exchange.market_type,
            )
        ]


def _bars(n: int) -> list[OHLCV]:
    out: list[OHLCV] = []
    for i in range(n):
        close = Decimal(str(100.0 + i))
        out.append(
            OHLCV(
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("10"),
                timestamp=T0 + i * H,
            )
        )
    return out


def _write_model(models_dir: Path, name: str, scale: float) -> None:
    meta = frozen_spec_metadata(list(DEFAULT_FEATURE_SPEC.feature_names), 3, 4)
    models_dir.mkdir(parents=True, exist_ok=True)
    (models_dir / f"{name}.onnx").write_bytes(confidence_onnx(meta, N_FEATURES, scale=scale))


def _cfg(threshold: float, shadow: bool, model: str) -> StrategyConfig:
    return StrategyConfig(
        id="seam",
        name="seam",
        strategy_class="unused.direct.class.passed",
        symbols=["BTCUSDT"],
        timeframes={"primary": "1h"},
        ml_overlay=MLOverlayConfig(
            enabled=True,
            model_name=model,
            confidence_threshold=threshold,
            shadow=shadow,
        ),
    )


async def _run(threshold: float, shadow: bool, model: str) -> Any:
    runner = BacktestRunner()
    return await runner.run(
        RisingEntry,
        _cfg(threshold, shadow, model),
        _bars(WARM + TRADED),
        initial_capital=Decimal("10000"),
        symbol="BTCUSDT",
        timeframe=Timeframe.H1,
        warmup_bars=WARM,
    )


class TestTheSeam:
    async def test_a_confident_entry_fills_through_the_seam(self, tmp_path: Path) -> None:
        # permissive model: rising bars -> RSI 100 -> P(up) = 1.0 >= 0.6
        _write_model(tmp_path, "permissive", 0.01)
        await attach_artifact(tmp_path, "permissive.onnx", "permissive")
        result = await _run(threshold=0.6, shadow=False, model="permissive")
        assert len(result.trades) == 1
        assert result.trades[0].symbol == "BTCUSDT"

    async def test_a_low_confidence_entry_is_vetoed_never_filled(self, tmp_path: Path) -> None:
        # strict model: rising bars -> RSI 100 -> P(up) = 0.1 < 0.6
        _write_model(tmp_path, "strict", 0.001)
        await attach_artifact(tmp_path, "strict.onnx", "strict")
        result = await _run(threshold=0.6, shadow=False, model="strict")
        assert result.trades == []

    async def test_at_the_threshold_the_entry_passes(self, tmp_path: Path) -> None:
        # permissive model: P(up) = 1.0 exactly == the maximum threshold
        _write_model(tmp_path, "permissive", 0.01)
        await attach_artifact(tmp_path, "permissive.onnx", "permissive")
        result = await _run(threshold=1.0, shadow=False, model="permissive")
        assert len(result.trades) == 1

    async def test_shadow_mode_fills_the_entry_it_would_have_vetoed(self, tmp_path: Path) -> None:
        # strict model, shadow on: the would-veto is recorded, the entry fills
        _write_model(tmp_path, "strict", 0.001)
        await attach_artifact(tmp_path, "strict.onnx", "strict")
        result = await _run(threshold=0.6, shadow=True, model="strict")
        assert len(result.trades) == 1

    async def test_a_missing_model_fails_closed_and_loud_no_silent_pass(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from hydra.core.logging import setup_logging

        setup_logging("INFO", "json")
        # nothing attached and the runner's runtime has no pool: the attach
        # refuses, the entry is vetoed (the §7 mount-absent shape, loud)
        result = await _run(threshold=0.6, shadow=False, model="ghost")
        assert result.trades == []
        out = capsys.readouterr().out
        assert '"event": "ml_overlay_failed"' in out
        assert "ghost" in out

    async def test_the_strict_model_answers_a_known_number(self, tmp_path: Path) -> None:
        # the pinned P(up) the veto above keys on: RSI(14) = 100 -> 0.1
        from hydra.ml.consumers import live_vector

        _write_model(tmp_path, "strict", 0.001)
        attached = await attach_artifact(tmp_path, "strict.onnx", "strict")
        p_up = attached.predict_up(live_vector(_bars(WARM + 1), [], []))
        assert p_up == pytest.approx(0.1, abs=1e-7)
        del attached
