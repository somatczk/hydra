"""Walk-forward optimisation.

Per fold: optimise on the in-sample window with the caller's parameter space, freeze
the best parameters, then run them out-of-sample warmed up with ``required_history``
in-sample bars (the warm-up bars only seed the context: no on_bar, no orders, no
equity points). The out-of-sample equity curves are stitched with their timestamps
and aggregated; the result carries the probabilistic Sharpe of the stitched curve and
the OOS/IS efficiency. Rolling (default) and anchored (IS always starts at bar 0)
windows are supported. A fold in which no parameter set trades in-sample fails the
whole run with an error naming the fold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.hyperopt import (
    SEED,
    HyperoptRunner,
    NoEligibleTrialError,
    ParameterSpace,
)
from hydra.backtest.metrics import BacktestResult, calculate_metrics
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.validation import daily_returns, probabilistic_sharpe
from hydra.core.types import OHLCV, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FoldResult:
    """One walk-forward fold: the selected config's IS run and its OOS run."""

    fold_index: int
    in_sample_metrics: BacktestResult
    out_of_sample_metrics: BacktestResult
    selected_params: dict[str, Any] = field(default_factory=dict)
    n_trials: int = 0
    is_start: datetime | None = None
    is_end: datetime | None = None
    oos_start: datetime | None = None
    oos_end: datetime | None = None


@dataclass(slots=True)
class WalkForwardResult:
    """Aggregated walk-forward analysis result."""

    folds: list[FoldResult] = field(default_factory=list)
    aggregated_oos_metrics: BacktestResult | None = None
    # probabilistic Sharpe of the stitched out-of-sample equity curve
    oos_psr: float | None = None
    # mean OOS Sharpe / mean IS Sharpe, when the mean IS Sharpe is positive
    efficiency: float | None = None
    anchored: bool = False
    seed: int = SEED

    def to_dict(self) -> dict[str, Any]:
        """The JSON payload stored on the job row; ids and provenance are added by the caller."""
        folds = [
            {
                "fold_index": f.fold_index,
                "in_sample_sharpe": f.in_sample_metrics.sharpe_ratio,
                "in_sample_return": float(f.in_sample_metrics.total_return),
                "in_sample_drawdown": float(f.in_sample_metrics.max_drawdown),
                "in_sample_trades": f.in_sample_metrics.total_trades,
                "out_of_sample_sharpe": f.out_of_sample_metrics.sharpe_ratio,
                "out_of_sample_return": float(f.out_of_sample_metrics.total_return),
                "out_of_sample_drawdown": float(f.out_of_sample_metrics.max_drawdown),
                "out_of_sample_trades": f.out_of_sample_metrics.total_trades,
                "selected_params": f.selected_params,
                "n_trials": f.n_trials,
                "is_start": _iso(f.is_start),
                "is_end": _iso(f.is_end),
                "oos_start": _iso(f.oos_start),
                "oos_end": _iso(f.oos_end),
            }
            for f in self.folds
        ]
        agg = self.aggregated_oos_metrics
        return {
            "folds": folds,
            "aggregated_sharpe": agg.sharpe_ratio if agg is not None else None,
            "aggregated_return": float(agg.total_return) if agg is not None else None,
            "aggregated_drawdown": float(agg.max_drawdown) if agg is not None else None,
            "out_of_sample_equity": (
                [float(eq) for eq in agg.equity_curve] if agg is not None else []
            ),
            "oos_psr": self.oos_psr,
            "efficiency": self.efficiency,
            "anchored": self.anchored,
            "seed": self.seed,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def window_bar_counts(
    bar_count: int, total_days: int, in_sample_months: int, out_of_sample_months: int
) -> tuple[int, int]:
    """In-sample / out-of-sample bar counts for 30-day months at the bars' real density."""
    bars_per_day = bar_count / total_days
    return (
        int(in_sample_months * 30 * bars_per_day),
        int(out_of_sample_months * 30 * bars_per_day),
    )


# ---------------------------------------------------------------------------
# Walk-forward Analyzer
# ---------------------------------------------------------------------------


class WalkForwardAnalyzer:
    """Performs walk-forward optimisation: per-fold re-optimisation, warm OOS runs."""

    def __init__(
        self,
        commission: CommissionConfig | None = None,
        initial_capital: Decimal = Decimal("100000"),
    ) -> None:
        self._commission = commission or CommissionConfig()
        self._initial_capital = initial_capital
        self._runner = BacktestRunner()

    async def run_walkforward(
        self,
        bars: list[OHLCV],
        strategy_class: type[BaseStrategy],
        strategy_config: StrategyConfig,
        param_space: ParameterSpace,
        in_sample_months: int = 6,
        out_of_sample_months: int = 2,
        method: Literal["bayesian", "grid"] = "grid",
        max_trials: int = 20,
        anchored: bool = False,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
    ) -> WalkForwardResult:
        """Run rolling walk-forward optimisation.

        Slides a window forward by ``out_of_sample_months`` at each step. Each window
        optimises on ``in_sample_months`` of training data (anchored windows always
        train from the first bar) and runs the selected config on the following
        ``out_of_sample_months`` of test data, warmed up from the train tail.
        ``max_trials`` caps the trials per fold (grid search is additionally capped by
        the true combination count).
        """
        empty = WalkForwardResult(anchored=anchored)
        if not bars:
            return empty

        # Approximate months to bar counts using bar timestamps
        total_days = (bars[-1].timestamp - bars[0].timestamp).days
        if total_days <= 0:
            return empty

        is_bars, oos_bars = window_bar_counts(
            len(bars), total_days, in_sample_months, out_of_sample_months
        )
        step_bars = oos_bars
        window_bars = is_bars + oos_bars

        if is_bars <= 0 or oos_bars <= 0 or window_bars > len(bars):
            return empty

        hyperopt = HyperoptRunner(self._runner, commission=self._commission)
        folds: list[FoldResult] = []
        fold_idx = 0
        start = 0

        while start + window_bars <= len(bars):
            is_start_idx = 0 if anchored else start
            is_end = start + is_bars
            oos_end = start + window_bars

            is_data = bars[is_start_idx:is_end]
            oos_data = bars[is_end:oos_end]

            try:
                best = await hyperopt.optimise(
                    strategy_class=strategy_class,
                    base_config=strategy_config,
                    bars=is_data,
                    initial_capital=self._initial_capital,
                    param_space=param_space,
                    method=method,
                    max_trials=max_trials,
                    symbol=symbol,
                    timeframe=timeframe,
                )
            except NoEligibleTrialError as exc:
                raise ValueError(f"fold {fold_idx}: no parameter set traded in-sample") from exc

            best_config = strategy_config.model_copy(
                update={"parameters": {**strategy_config.parameters, **best.best_params}}
            )
            required_history = int(
                strategy_class(config=best_config, context=StrategyContext()).required_history
            )
            warm = bars[max(0, is_end - required_history) : is_end] if required_history > 0 else []
            oos_result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=best_config,
                bars=[*warm, *oos_data],
                initial_capital=self._initial_capital,
                commission=self._commission,
                symbol=symbol,
                timeframe=timeframe,
                warmup_bars=len(warm),
            )

            folds.append(
                FoldResult(
                    fold_index=fold_idx,
                    in_sample_metrics=best.best_result or BacktestResult(),
                    out_of_sample_metrics=oos_result,
                    selected_params=best.best_params,
                    n_trials=best.n_trials,
                    is_start=is_data[0].timestamp,
                    is_end=is_data[-1].timestamp,
                    oos_start=oos_data[0].timestamp,
                    oos_end=oos_data[-1].timestamp,
                )
            )

            fold_idx += 1
            start += step_bars

        if not folds:
            return empty

        # Stitch the OOS equity curves: each fold starts a fresh session at the
        # analyzer's capital, so every point — the first bar's P&L included —
        # re-bases through that capital onto the previous fold's ending value.
        step = timedelta(seconds=timeframe_seconds(timeframe))
        all_oos_equity: list[Decimal] = [self._initial_capital]
        all_oos_timestamps: list[datetime] = [folds[0].out_of_sample_metrics.timestamps[0] - step]
        all_oos_trades = []
        for fold in folds:
            oos = fold.out_of_sample_metrics
            if oos.equity_curve and self._initial_capital != 0:
                base = all_oos_equity[-1]
                for eq, ts in zip(oos.equity_curve, oos.timestamps, strict=True):
                    all_oos_equity.append(base * eq / self._initial_capital)
                    all_oos_timestamps.append(ts)
            all_oos_trades.extend(oos.trades)

        agg_metrics = calculate_metrics(
            equity_curve=all_oos_equity,
            trades=all_oos_trades,
            timeframe=timeframe,
            timestamps=all_oos_timestamps,
        )
        oos_psr = probabilistic_sharpe(daily_returns(all_oos_equity, all_oos_timestamps))
        is_sharpes = [f.in_sample_metrics.sharpe_ratio for f in folds]
        oos_sharpes = [f.out_of_sample_metrics.sharpe_ratio for f in folds]
        mean_is = sum(is_sharpes) / len(is_sharpes)
        mean_oos = sum(oos_sharpes) / len(oos_sharpes)

        return WalkForwardResult(
            folds=folds,
            aggregated_oos_metrics=agg_metrics,
            oos_psr=oos_psr,
            efficiency=mean_oos / mean_is if mean_is > 0 else None,
            anchored=anchored,
            seed=SEED,
        )
