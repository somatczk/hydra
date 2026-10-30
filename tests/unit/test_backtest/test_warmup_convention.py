"""Warm-up is replayed, never scored: every run measures only the bars its strategy can trade.

The strategy gates itself on ``required_history`` like the rule-based one, but trades on a
fixed calendar (enter at cycle position 0, exit at 5), so two lookbacks behave identically
on every bar both can trade. Expected numbers are computed here from a warm-up-free run's
own equity curve, not from the code under test.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hydra.backtest.hyperopt import HyperoptRunner, ParamDef, ParameterSpace
from hydra.backtest.metrics import BacktestResult, calculate_metrics
from hydra.backtest.runner import BacktestRunner, strategy_warmup
from hydra.backtest.walkforward import WalkForwardAnalyzer
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import OHLCV, Direction, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

D = Decimal
DAY = timedelta(days=1)
T0 = datetime(2024, 1, 1, tzinfo=UTC)
CAPITAL = D("10000")


def _bars(count: int) -> list[OHLCV]:
    """Daily bars: a 10-bar sawtooth (3 % swing, under the 5 % stop) on a slow uptrend."""
    out: list[OHLCV] = []
    for i in range(count):
        pos = i % 10
        tri = pos if pos < 5 else 9 - pos
        price = D("100") + D("0.75") * tri + D("0.05") * i
        out.append(
            OHLCV(
                open=price,
                high=price + D("0.05"),
                low=price - D("0.05"),
                close=price,
                volume=D("1000"),
                timestamp=T0 + i * DAY,
            )
        )
    return out


class _CalendarStrategy(BaseStrategy):
    """Silent until the context holds ``rh`` bars; then enters at position 0, exits at 5."""

    @property
    def required_history(self) -> int:
        return int(self.config.parameters["rh"])

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if len(self.context.bars(str(bar.symbol), bar.timeframe, self.required_history)) < (
            self.required_history
        ):
            return []
        pos = int((bar.ohlcv.timestamp - T0).total_seconds() // 86_400) % 10
        held = self.context.position(str(bar.symbol)) is not None
        if pos == 0 and not held:
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=D("1"),
                    strategy_id=self.strategy_id,
                )
            ]
        if pos == 5 and held:
            return [ExitSignal(symbol=bar.symbol, strategy_id=self.strategy_id)]
        return []


def _config(rh: int) -> StrategyConfig:
    return StrategyConfig(
        id="warm",
        name="Warm",
        strategy_class="test._CalendarStrategy",
        symbols=["BTCUSDT"],
        parameters={"rh": rh},
    )


def _space(*choices: int) -> ParameterSpace:
    return ParameterSpace(params=[ParamDef(name="rh", type="categorical", choices=list(choices))])


async def _run(bars: list[OHLCV], rh: int, *, warmup_bars: int) -> BacktestResult:
    return await BacktestRunner().run(
        strategy_class=_CalendarStrategy,
        strategy_config=_config(rh),
        bars=bars,
        initial_capital=CAPITAL,
        timeframe=Timeframe.D1,
        warmup_bars=warmup_bars,
    )


def _sharpe_over(equity: list[Decimal]) -> float:
    return calculate_metrics(equity, [], timeframe=Timeframe.D1).sharpe_ratio


def _trade_keys(result: BacktestResult) -> list[tuple[datetime, datetime, Decimal]]:
    return [(t.entry_time, t.exit_time, t.pnl) for t in result.trades]


async def test_the_helper_is_the_instances_required_history() -> None:
    assert strategy_warmup(_CalendarStrategy, _config(37)) == 37


async def test_a_plain_run_scores_only_the_post_warmup_bars() -> None:
    bars = _bars(200)
    rh = 20
    diluted = await _run(bars, rh, warmup_bars=0)
    warm = await _run(bars, rh, warmup_bars=strategy_warmup(_CalendarStrategy, _config(rh)))

    assert len(diluted.equity_curve) == 200
    assert len(warm.equity_curve) == 200 - rh
    assert warm.timestamps == [b.timestamp for b in bars[rh:]]
    assert _trade_keys(warm) == _trade_keys(diluted)
    assert warm.total_trades > 0
    # flat before the warm-up ends, so the scored bars are the diluted run's tail
    assert warm.equity_curve == diluted.equity_curve[rh:]
    assert warm.sharpe_ratio == _sharpe_over(diluted.equity_curve[rh:])
    assert warm.sharpe_ratio != diluted.sharpe_ratio
    assert warm.benchmark_equity[0] == CAPITAL  # buy-and-hold starts on the first scored bar
    assert len(warm.benchmark_equity) == 200 - rh


async def test_a_hyperopt_trial_is_scored_without_its_warmup() -> None:
    bars = _bars(200)
    result = await HyperoptRunner().optimise(
        strategy_class=_CalendarStrategy,
        base_config=_config(20),
        bars=bars,
        initial_capital=CAPITAL,
        param_space=_space(20),
        method="grid",
        timeframe=Timeframe.D1,
    )
    reference = await _run(bars, 20, warmup_bars=20)

    assert result.best_result is not None
    assert len(result.best_result.equity_curve) == 180
    assert result.best_metric == reference.sharpe_ratio
    assert _trade_keys(result.best_result) == _trade_keys(reference)


async def test_lookbacks_with_identical_behaviour_score_the_same_on_a_common_window() -> None:
    bars = _bars(240)
    result = await HyperoptRunner().optimise(
        strategy_class=_CalendarStrategy,
        base_config=_config(20),
        bars=bars[60:],
        initial_capital=CAPITAL,
        param_space=_space(20, 50),
        method="grid",
        timeframe=Timeframe.D1,
        lead_bars=bars[:60],
    )

    short, long = result.trials
    assert (short.params, long.params) == ({"rh": 20}, {"rh": 50})
    assert short.total_trades == long.total_trades > 0
    assert short.sharpe == long.sharpe
    assert short.total_return == long.total_return


async def test_a_trial_whose_warmup_consumes_the_window_is_ineligible_not_a_crash() -> None:
    result = await HyperoptRunner().optimise(
        strategy_class=_CalendarStrategy,
        base_config=_config(20),
        bars=_bars(100),
        initial_capital=CAPITAL,
        param_space=_space(500, 20),
        method="grid",
        timeframe=Timeframe.D1,
    )

    assert [t.status for t in result.trials] == ["no_trades", "ok"]
    assert result.best_params == {"rh": 20}


async def test_walkforward_in_sample_warms_from_the_bars_before_the_fold() -> None:
    bars = _bars(240)
    result = await WalkForwardAnalyzer(initial_capital=CAPITAL).run_walkforward(
        bars=bars,
        strategy_class=_CalendarStrategy,
        strategy_config=_config(20),
        param_space=_space(20),
        fold_count=3,
        train_pct=0.5,
        timeframe=Timeframe.D1,
    )

    in_sample = [len(f.in_sample_metrics.equity_curve) for f in result.folds]
    assert in_sample == [60 - 20, 60, 60]  # fold 0 has nothing before it; the rest warm from it
    fold_one = await _run(bars[40:120], 20, warmup_bars=20)
    assert result.folds[1].in_sample_metrics.sharpe_ratio == fold_one.sharpe_ratio


async def test_the_holdout_and_each_fold_carry_buy_and_hold_over_their_traded_bars() -> None:
    bars = _bars(300)
    hyper = await HyperoptRunner().run(
        strategy_class=_CalendarStrategy,
        base_config=_config(20),
        bars=bars,
        initial_capital=CAPITAL,
        param_space=_space(20),
        method="grid",
        timeframe=Timeframe.D1,
    )
    holdout_bars = bars[240:]
    payload: dict[str, Any] = hyper.to_dict()["holdout"]
    assert hyper.holdout is not None
    assert hyper.holdout.timestamps == [b.timestamp for b in holdout_bars]
    expected = float(holdout_bars[-1].close / holdout_bars[0].close - 1)
    assert payload["benchmark_return"] == float(hyper.holdout.benchmark_return)
    assert abs(payload["benchmark_return"] - expected) < 1e-9
    held = calculate_metrics(
        [CAPITAL * b.close / holdout_bars[0].close for b in holdout_bars],
        [],
        timeframe=Timeframe.D1,
    )
    assert payload["benchmark_sharpe"] == held.sharpe_ratio

    walk = await WalkForwardAnalyzer(initial_capital=CAPITAL).run_walkforward(
        bars=bars,
        strategy_class=_CalendarStrategy,
        strategy_config=_config(20),
        param_space=_space(20),
        fold_count=3,
        train_pct=0.5,
        timeframe=Timeframe.D1,
    )
    for fold, row in zip(walk.folds, walk.to_dict()["folds"], strict=True):
        assert fold.oos_start is not None
        assert fold.oos_end is not None
        window = [b for b in bars if fold.oos_start <= b.timestamp <= fold.oos_end]
        expected = float(window[-1].close / window[0].close - 1)
        assert abs(row["out_of_sample_benchmark_return"] - expected) < 1e-9
        assert row["out_of_sample_benchmark_sharpe"] == fold.out_of_sample_metrics.benchmark_sharpe
