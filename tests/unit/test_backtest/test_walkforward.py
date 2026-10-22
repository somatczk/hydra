"""WalkForwardAnalyzer: per-fold re-optimisation, warm OOS runs, stitched OOS results.

The regime-change fixture is a 10-bar sawtooth whose rising and falling halves swap at
the middle of the data: a ``phase`` parameter that buys at cycle position 0 (exits at 5)
wins in-sample in the first half, and the mirrored ``phase`` 5 wins in the second.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, ClassVar

import pytest

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.hyperopt import ParamDef, ParameterSpace
from hydra.backtest.walkforward import (
    WalkForwardAnalyzer,
    WalkForwardResult,
    window_bar_counts,
)
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2024, 1, 1, tzinfo=UTC)
UNIT = D("0.75")  # 3 % swing per sawtooth half-cycle, below the 5 % protective stop


def _saw_bars(count: int) -> list[OHLCV]:
    """Daily 10-bar sawtooth cycles; the ramp direction flips at the data's midpoint."""
    bars: list[OHLCV] = []
    for i in range(count):
        pos = i % 10
        tri = pos if pos < 5 else 9 - pos  # 0,1,2,3,4,4,3,2,1,0
        ramp = UNIT * tri if i < count // 2 else UNIT * (4 - tri)
        price = D("100") + ramp
        bars.append(
            OHLCV(
                open=price,
                high=price + D("0.05"),
                low=price - D("0.05"),
                close=price,
                volume=D("1000"),
                timestamp=T0 + i * DAY,
            )
        )
    return bars


def _flat_bars(count: int) -> list[OHLCV]:
    return [
        OHLCV(
            open=D("100"),
            high=D("100"),
            low=D("100"),
            close=D("100"),
            volume=D("1000"),
            timestamp=T0 + i * DAY,
        )
        for i in range(count)
    ]


def _config(**overrides: Any) -> StrategyConfig:
    data: dict[str, Any] = {
        "id": "wf",
        "name": "WF",
        "strategy_class": "test._PhaseStrategy",
        "symbols": ["BTCUSDT"],
        "parameters": {"phase": 0},
    }
    data.update(overrides)
    return StrategyConfig(**data)


class _PhaseStrategy(BaseStrategy):
    """Enters at cycle position ``phase`` and exits 5 bars later."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        idx = int((bar.ohlcv.timestamp - T0).total_seconds() // 86_400)
        pos = idx % 10
        phase = int(self.config.parameters.get("phase", 0)) % 10
        has_position = self.context.position(str(bar.symbol)) is not None
        if pos == phase and not has_position:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                )
            ]
        if pos == (phase + 5) % 10 and has_position:
            return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]
        return []


class _NeedsHistoryStrategy(BaseStrategy):
    """Enters only once the context holds ``required_history`` bars, exits 5 bars later."""

    @property
    def required_history(self) -> int:
        return 20

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        history = self.context.bars(str(bar.symbol), bar.timeframe, 10_000)
        if len(history) < 20:
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
        if len(history) >= 25:
            return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]
        return []


class _RecordingStrategy(_PhaseStrategy):
    """Phase strategy that records the timestamp of every on_bar call."""

    seen: ClassVar[list[datetime]] = []

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        type(self).seen.append(bar.ohlcv.timestamp)
        return await super().on_bar(bar)


def _phase_space(*choices: int) -> ParameterSpace:
    return ParameterSpace(
        params=[ParamDef(name="phase", type="categorical", choices=list(choices))]
    )


@pytest.fixture
def analyzer() -> WalkForwardAnalyzer:
    return WalkForwardAnalyzer(initial_capital=D("10000"))


async def _run_sawtooth(
    analyzer: WalkForwardAnalyzer, *, anchored: bool = False
) -> WalkForwardResult:
    """240 daily bars, 60-bar IS and 60-bar OOS windows stepping 60: three folds."""
    return await analyzer.run_walkforward(
        bars=_saw_bars(240),
        strategy_class=_PhaseStrategy,
        strategy_config=_config(),
        param_space=_phase_space(0, 5),
        in_sample_months=2,
        out_of_sample_months=2,
        anchored=anchored,
        timeframe=Timeframe.D1,
    )


class TestRegimeChange:
    async def test_reoptimises_per_fold_on_regime_change(
        self, analyzer: WalkForwardAnalyzer
    ) -> None:
        result = await _run_sawtooth(analyzer)

        assert [f.selected_params for f in result.folds] == [
            {"phase": 0},
            {"phase": 0},
            {"phase": 5},
        ]
        for fold in result.folds:
            assert fold.n_trials == 2
            assert fold.in_sample_metrics.total_trades >= 1
            assert fold.out_of_sample_metrics.total_trades >= 1
        # the frozen params carry the regime: fold 1 selects on the first half and its
        # OOS (second half) loses; fold 2 re-optimises on the second half and wins again
        returns = [float(f.out_of_sample_metrics.total_return) for f in result.folds]
        assert returns[0] > 0
        assert returns[1] < 0
        assert returns[2] > 0

    async def test_oos_is_warmed_up_not_cold(self, analyzer: WalkForwardAnalyzer) -> None:
        result = await analyzer.run_walkforward(
            bars=_flat_bars(240),
            strategy_class=_NeedsHistoryStrategy,
            strategy_config=_config(parameters={"phase": 0}),
            param_space=_phase_space(0),
            in_sample_months=2,
            out_of_sample_months=2,
            timeframe=Timeframe.D1,
        )
        assert len(result.folds) == 3

        for fold in result.folds:
            oos = fold.out_of_sample_metrics
            # one equity point per OOS bar: the 20 warm-up bars produce none
            assert len(oos.equity_curve) == 60
            assert len(oos.timestamps) == 60
            assert oos.total_trades >= 1
            # a cold start could not trade before OOS bar 20; the warm-up lets the first
            # entry land on one of the first two OOS bars
            first_entry = min(t.entry_time for t in oos.trades)
            oos_start = fold.oos_start
            assert oos_start is not None
            assert oos_start <= first_entry <= oos_start + 2 * DAY

    async def test_windows_never_overlap_and_anchored_starts_at_zero(
        self, analyzer: WalkForwardAnalyzer
    ) -> None:
        _RecordingStrategy.seen.clear()
        rolling = await _run_sawtooth(analyzer)

        bars = _saw_bars(240)
        for k, fold in enumerate(rolling.folds):
            assert fold.is_end is not None and fold.oos_start is not None
            assert fold.is_end <= fold.oos_start
            assert fold.is_start == bars[k * 60].timestamp
            assert fold.oos_start == bars[k * 60 + 60].timestamp
            assert fold.oos_end == bars[k * 60 + 119].timestamp

        anchored = await analyzer.run_walkforward(
            bars=bars,
            strategy_class=_RecordingStrategy,
            strategy_config=_config(),
            param_space=_phase_space(0, 5),
            in_sample_months=2,
            out_of_sample_months=2,
            anchored=True,
            timeframe=Timeframe.D1,
        )
        # anchored IS windows always start at bar 0, so fold 2 trains on 120 first-half
        # bars and only 60 second-half bars and still selects phase 0: anchored
        # optimisation lags the regime change that the rolling windows catch
        assert [f.selected_params for f in anchored.folds] == [
            {"phase": 0},
            {"phase": 0},
            {"phase": 0},
        ]
        for fold in anchored.folds:
            assert fold.is_start == bars[0].timestamp
            assert fold.is_end is not None and fold.oos_start is not None
            assert fold.is_end <= fold.oos_start

        # every on_bar call happened inside some fold's IS or OOS window
        windows = set()
        for result in (rolling, anchored):
            for fold in result.folds:
                assert fold.is_start is not None and fold.oos_end is not None
                windows |= {
                    b.timestamp for b in bars if fold.is_start <= b.timestamp <= fold.oos_end
                }
        assert set(_RecordingStrategy.seen) <= windows

    async def test_stitched_oos_has_timestamps(self, analyzer: WalkForwardAnalyzer) -> None:
        result = await _run_sawtooth(analyzer)

        agg = result.aggregated_oos_metrics
        assert agg is not None
        assert len(agg.monthly_returns) >= 6  # 240 daily bars span eight months
        assert len(agg.timestamps) == len(agg.equity_curve)
        assert isinstance(result.oos_psr, float)
        assert result.efficiency is not None and result.efficiency > 0
        assert result.seed == 42

    async def test_stitched_oos_compounds_every_folds_first_bar(
        self, analyzer: WalkForwardAnalyzer
    ) -> None:
        result = await _run_sawtooth(analyzer)
        agg = result.aggregated_oos_metrics
        assert agg is not None

        # one stitched point per OOS bar of every fold, plus the starting capital
        assert len(agg.equity_curve) == 1 + sum(
            len(f.out_of_sample_metrics.equity_curve) for f in result.folds
        )
        # no fold boundary drops a bar: consecutive timestamps are always one day apart
        assert {b - a for a, b in itertools.pairwise(agg.timestamps)} == {DAY}

        # every fold re-bases through its own starting capital, its first OOS bar's
        # P&L included, so the stitched end is the compounded per-fold end equity
        capital = agg.equity_curve[0]
        expected = capital
        for fold in result.folds:
            expected = expected * fold.out_of_sample_metrics.equity_curve[-1] / capital
        assert float(agg.equity_curve[-1]) == pytest.approx(float(expected), rel=1e-9)


class TestCommissionPlumbing:
    async def test_is_optimisation_runs_with_the_analyzers_commission(self) -> None:
        """The analyzer's commission prices both windows: IS trials and OOS runs."""
        default = await _run_sawtooth(WalkForwardAnalyzer(initial_capital=D("10000")))
        dearer = await _run_sawtooth(
            WalkForwardAnalyzer(
                commission=CommissionConfig(spot_taker=D("0.01")), initial_capital=D("10000")
            )
        )

        for d_fold, c_fold in zip(default.folds, dearer.folds, strict=True):
            default_is = sum(float(t.fees) for t in d_fold.in_sample_metrics.trades)
            dearer_is = sum(float(t.fees) for t in c_fold.in_sample_metrics.trades)
            default_oos = sum(float(t.fees) for t in d_fold.out_of_sample_metrics.trades)
            dearer_oos = sum(float(t.fees) for t in c_fold.out_of_sample_metrics.trades)
            # the 10x taker fee scales both windows' fees, not just the OOS one
            assert dearer_is == pytest.approx(10.0 * default_is, rel=0.05)
            assert dearer_oos == pytest.approx(10.0 * default_oos, rel=0.05)


class _IdleStrategy(BaseStrategy):
    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        return []


class TestFailureModes:
    async def test_fold_without_an_eligible_config_fails_the_run(
        self, analyzer: WalkForwardAnalyzer
    ) -> None:
        with pytest.raises(ValueError, match="fold 0: no parameter set traded in-sample"):
            await analyzer.run_walkforward(
                bars=_saw_bars(240),
                strategy_class=_IdleStrategy,
                strategy_config=_config(),
                param_space=_phase_space(0, 5),
                in_sample_months=2,
                out_of_sample_months=2,
                timeframe=Timeframe.D1,
            )

    async def test_unknown_param_name_fails_the_run(self, analyzer: WalkForwardAnalyzer) -> None:
        with pytest.raises(ValueError, match="fase"):
            await analyzer.run_walkforward(
                bars=_saw_bars(240),
                strategy_class=_PhaseStrategy,
                strategy_config=_config(),
                param_space=ParameterSpace(
                    params=[ParamDef(name="fase", type="categorical", choices=[0, 5])]
                ),
                in_sample_months=2,
                out_of_sample_months=2,
                timeframe=Timeframe.D1,
            )

    async def test_empty_bars(self, analyzer: WalkForwardAnalyzer) -> None:
        result = await analyzer.run_walkforward(
            bars=[],
            strategy_class=_PhaseStrategy,
            strategy_config=_config(),
            param_space=_phase_space(0),
        )
        assert result.folds == []
        assert result.aggregated_oos_metrics is None
        assert result.oos_psr is None

    async def test_range_too_short(self, analyzer: WalkForwardAnalyzer) -> None:
        result = await analyzer.run_walkforward(
            bars=_saw_bars(30),
            strategy_class=_PhaseStrategy,
            strategy_config=_config(),
            param_space=_phase_space(0),
            in_sample_months=2,
            out_of_sample_months=2,
            timeframe=Timeframe.D1,
        )
        assert result.folds == []


def test_window_bar_counts_at_real_density() -> None:
    # 240 daily bars over 239 days: 2 months of IS and OOS are 60 bars each
    assert window_bar_counts(240, 239, 2, 2) == (60, 60)
    # weekly bars: one month is ~4.3 bars
    assert window_bar_counts(57, 392, 4, 2) == (17, 8)
