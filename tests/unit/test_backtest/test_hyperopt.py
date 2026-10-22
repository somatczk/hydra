"""HyperoptRunner: trial eligibility, config preservation, the holdout split, reproducibility.

The scenarios reproduce the BT-8 audit findings: a crashing or never-trading config used
to win the grid, and trial configs used to drop position_sizing / filters / risk.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from hydra.backtest.hyperopt import (
    SEED,
    HyperoptResult,
    HyperoptRunner,
    NoEligibleTrialError,
    ParamDef,
    ParameterSpace,
)
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

D = Decimal
T0 = datetime(2024, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


def _bars(count: int, step: float = -0.2) -> list[OHLCV]:
    """Hourly bars on a slow drift with enough volume for any entry."""
    out: list[OHLCV] = []
    price = 100.0
    for i in range(count):
        close = D(str(round(price, 2)))
        out.append(
            OHLCV(
                open=close,
                high=close + D("0.1"),
                low=close - D("0.1"),
                close=close,
                volume=D("1000"),
                timestamp=T0 + i * H,
            )
        )
        price += step
    return out


def _config(**overrides: Any) -> StrategyConfig:
    data: dict[str, Any] = {
        "id": "hyper",
        "name": "Hyper",
        "strategy_class": "test._ModeStrategy",
        "symbols": ["BTCUSDT"],
        "parameters": {"mode": "trade"},
    }
    data.update(overrides)
    return StrategyConfig(**data)


class _ModeStrategy(BaseStrategy):
    """'trade' alternates entry/exit, 'raise' crashes on the first bar, 'flat' never trades."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        mode = str(self.config.parameters.get("mode", "trade"))
        if mode == "raise":
            raise RuntimeError("boom")
        if mode == "flat":
            return []
        if self.context.position(str(bar.symbol)) is None:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                )
            ]
        return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]


class _ConfigSpyStrategy(_ModeStrategy):
    """Records the sizing / filters / risk blocks it was actually built with, and trades."""

    seen: ClassVar[list[tuple[float, dict[str, Any], float]]] = []

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        type(self).seen.append(
            (
                self.config.position_sizing.max_position_pct,
                dict(self.config.filters),
                self.config.risk.stop_loss_value,
            )
        )
        return await super().on_bar(bar)


class _RecordingStrategy(BaseStrategy):
    """Alternates entry/exit; records every on_bar timestamp and each run's first context bar."""

    seen: ClassVar[list[datetime]] = []
    first_context: ClassVar[list[datetime]] = []

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        super().__init__(config, context)
        self._recorded_first = False

    @property
    def required_history(self) -> int:
        return 10

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        type(self).seen.append(bar.ohlcv.timestamp)
        if not self._recorded_first:
            self._recorded_first = True
            history = self.context.bars(str(bar.symbol), bar.timeframe, 10_000)
            type(self).first_context.append(history[0].timestamp)
        if self.context.position(str(bar.symbol)) is None:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                )
            ]
        return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]


class _CountingStrategy(_ModeStrategy):
    """Marks the first on_bar of each engine run, so repeated param sets can be counted."""

    runs: ClassVar[int] = 0

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        super().__init__(config, context)
        self._marked = False

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if not self._marked:
            self._marked = True
            type(self).runs += 1
        return await super().on_bar(bar)


def _mode_space(*choices: str) -> ParameterSpace:
    return ParameterSpace(params=[ParamDef(name="mode", type="categorical", choices=list(choices))])


async def test_crashing_and_idle_configs_never_win() -> None:
    bars = _bars(80)
    result = await HyperoptRunner().run(
        strategy_class=_ModeStrategy,
        base_config=_config(),
        bars=bars,
        initial_capital=D("10000"),
        param_space=_mode_space("trade", "raise", "flat"),
        method="grid",
        max_trials=10,
        timeframe=Timeframe.H1,
    )

    assert result.best_params == {"mode": "trade"}
    assert [t.status for t in result.trials] == ["ok", "failed", "no_trades"]
    failed = next(t for t in result.trials if t.status == "failed")
    assert "boom" in (failed.error or "")
    idle = next(t for t in result.trials if t.status == "no_trades")
    assert idle.total_trades == 0
    assert result.n_trials == 1
    assert result.failed_trials == 1
    assert result.holdout is not None
    assert result.holdout.total_trades >= 1


async def test_trial_keeps_the_whole_base_config() -> None:
    _ConfigSpyStrategy.seen.clear()
    config = _config(
        position_sizing={"max_position_pct": 2.0},
        filters={"f": 1},
        risk={"stop_loss_method": "fixed_pct", "stop_loss_value": 1.0},
    )
    result = await HyperoptRunner().run(
        strategy_class=_ConfigSpyStrategy,
        base_config=config,
        bars=_bars(60),
        initial_capital=D("10000"),
        param_space=_mode_space("trade"),
        method="grid",
        max_trials=5,
    )

    assert result.best_params == {"mode": "trade"}
    assert _ConfigSpyStrategy.seen
    # at main the trial config silently reset these to (10.0, {}, 5.0)
    assert all(t == (2.0, {"f": 1}, 1.0) for t in _ConfigSpyStrategy.seen)


async def test_optimiser_never_sees_the_holdout() -> None:
    _RecordingStrategy.seen.clear()
    _RecordingStrategy.first_context.clear()
    bars = _bars(500, step=0.1)
    result = await HyperoptRunner().run(
        strategy_class=_RecordingStrategy,
        base_config=_config(),
        bars=bars,
        initial_capital=D("10000"),
        param_space=_mode_space("trade"),
        method="grid",
        max_trials=5,
    )

    # 400 trial bars + 100 holdout bars; the 10 warm-up bars never reach on_bar
    assert len(_RecordingStrategy.seen) == 500
    assert all(ts < bars[400].timestamp for ts in _RecordingStrategy.seen[:400])
    assert all(ts >= bars[400].timestamp for ts in _RecordingStrategy.seen[400:])
    # the holdout run starts with the 10 train-tail bars already in its context
    assert _RecordingStrategy.first_context == [bars[0].timestamp, bars[390].timestamp]
    assert result.train_start == bars[0].timestamp
    assert result.train_end == bars[399].timestamp
    assert result.holdout_start == bars[400].timestamp
    assert result.holdout_end == bars[-1].timestamp
    assert result.holdout is not None
    assert result.holdout.total_trades >= 1
    assert all(t.entry_time >= bars[400].timestamp for t in result.holdout.trades)


async def test_bayesian_is_reproducible() -> None:
    bars = _bars(120, step=0.0)
    space = ParameterSpace(params=[ParamDef(name="level", type="int", low=1, high=3)])

    async def _run() -> tuple[HyperoptResult, int]:
        _CountingStrategy.runs = 0
        result = await HyperoptRunner().run(
            strategy_class=_CountingStrategy,
            base_config=_config(),
            bars=bars,
            initial_capital=D("10000"),
            param_space=space,
            method="bayesian",
            max_trials=12,
        )
        return result, _CountingStrategy.runs

    first, first_runs = await _run()
    second, second_runs = await _run()

    assert [t.params for t in first.trials] == [t.params for t in second.trials]
    assert [t.sharpe for t in first.trials] == [t.sharpe for t in second.trials]
    assert first.dsr == second.dsr
    assert first.pbo == second.pbo
    assert first.holdout is not None and second.holdout is not None
    assert first.holdout.sharpe_ratio == second.holdout.sharpe_ratio
    assert first.seed == SEED == 42

    assert len(first.trials) == 12
    # TPE resamples parameter sets it has already tried; each distinct set runs once
    distinct = {json.dumps(t.params, sort_keys=True) for t in first.trials}
    assert len(distinct) == first.n_trials < 12
    assert first_runs == second_runs == first.n_trials + 1


async def test_no_eligible_trial_fails() -> None:
    with pytest.raises(NoEligibleTrialError):
        await HyperoptRunner().run(
            strategy_class=_ModeStrategy,
            base_config=_config(),
            bars=_bars(60),
            initial_capital=D("10000"),
            param_space=_mode_space("raise", "flat"),
            method="grid",
            max_trials=5,
        )
