"""Walk-forward analysis and Combinatorial Purged Cross-Validation (CPCV).

Provides rolling window walk-forward optimization and CPCV with purging,
embargo, and Probability of Backtest Overfitting (PBO) computation.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from decimal import Decimal

import numpy as np

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult, calculate_metrics
from hydra.backtest.runner import BacktestRunner
from hydra.core.types import OHLCV, Timeframe
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FoldResult:
    """Metrics for a single walk-forward fold."""

    fold_index: int
    in_sample_metrics: BacktestResult
    out_of_sample_metrics: BacktestResult


@dataclass(slots=True)
class WalkForwardResult:
    """Aggregated walk-forward analysis result."""

    folds: list[FoldResult] = field(default_factory=list)
    aggregated_oos_metrics: BacktestResult | None = None


@dataclass(slots=True)
class CPCVResult:
    """Result from Combinatorial Purged Cross-Validation."""

    fold_results: list[FoldResult] = field(default_factory=list)
    pbo_score: float = 0.0
    deflated_sharpe: float = 0.0
    path_sharpes: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Walk-forward Analyzer
# ---------------------------------------------------------------------------


class WalkForwardAnalyzer:
    """Performs walk-forward analysis and CPCV on backtest data."""

    def __init__(
        self,
        commission: CommissionConfig | None = None,
        initial_capital: Decimal = Decimal("100000"),
    ) -> None:
        self._commission = commission or CommissionConfig()
        self._initial_capital = initial_capital
        self._runner = BacktestRunner()

    # -- Walk-forward --------------------------------------------------------

    async def run_walkforward(
        self,
        bars: list[OHLCV],
        strategy_class: type[BaseStrategy],
        strategy_config: StrategyConfig,
        in_sample_months: int = 6,
        out_of_sample_months: int = 2,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
    ) -> WalkForwardResult:
        """Run rolling walk-forward analysis.

        Slides a window forward by ``out_of_sample_months`` at each step.
        Each window has ``in_sample_months`` of training data followed by
        ``out_of_sample_months`` of test data.
        """
        if not bars:
            return WalkForwardResult()

        # Approximate months to bar counts using bar timestamps
        total_days = (bars[-1].timestamp - bars[0].timestamp).days
        if total_days <= 0:
            return WalkForwardResult()

        bars_per_day = max(len(bars) / total_days, 1)
        is_bars = int(in_sample_months * 30 * bars_per_day)
        oos_bars = int(out_of_sample_months * 30 * bars_per_day)
        step_bars = oos_bars
        window_bars = is_bars + oos_bars

        if window_bars > len(bars):
            return WalkForwardResult()

        folds: list[FoldResult] = []
        fold_idx = 0
        start = 0

        while start + window_bars <= len(bars):
            is_end = start + is_bars
            oos_end = start + window_bars

            is_data = bars[start:is_end]
            oos_data = bars[is_end:oos_end]

            # Run in-sample backtest
            is_result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=strategy_config,
                bars=is_data,
                initial_capital=self._initial_capital,
                commission=self._commission,
                symbol=symbol,
                timeframe=timeframe,
            )

            # Run out-of-sample backtest
            oos_result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=strategy_config,
                bars=oos_data,
                initial_capital=self._initial_capital,
                commission=self._commission,
                symbol=symbol,
                timeframe=timeframe,
            )

            folds.append(
                FoldResult(
                    fold_index=fold_idx,
                    in_sample_metrics=is_result,
                    out_of_sample_metrics=oos_result,
                )
            )

            fold_idx += 1
            start += step_bars

        # Aggregate OOS metrics: concatenate OOS equity curves
        if folds:
            all_oos_equity: list[Decimal] = [self._initial_capital]
            all_oos_trades = []
            for fold in folds:
                oos = fold.out_of_sample_metrics
                if len(oos.equity_curve) > 1:
                    # Scale the OOS equity to continue from the previous ending value
                    base = all_oos_equity[-1]
                    start_eq = oos.equity_curve[0]
                    if start_eq != 0:
                        for eq in oos.equity_curve[1:]:
                            scaled = base * eq / start_eq
                            all_oos_equity.append(scaled)
                all_oos_trades.extend(oos.trades)

            agg_metrics = calculate_metrics(
                equity_curve=all_oos_equity,
                trades=all_oos_trades,
            )
        else:
            agg_metrics = None

        return WalkForwardResult(folds=folds, aggregated_oos_metrics=agg_metrics)

    # -- CPCV ----------------------------------------------------------------

    async def run_cpcv(
        self,
        bars: list[OHLCV],
        strategy_class: type[BaseStrategy],
        strategy_config: StrategyConfig,
        n_splits: int = 6,
        purge_days: int = 5,
        embargo_days: int = 2,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
    ) -> CPCVResult:
        """Run Combinatorial Purged Cross-Validation.

        Splits data into ``n_splits`` contiguous blocks. For each combination
        of 2 test blocks, the remaining blocks form the training set, with
        purging and embargo applied.

        Parameters
        ----------
        bars:
            Chronologically sorted OHLCV bars.
        strategy_class:
            Strategy class to evaluate.
        strategy_config:
            Strategy configuration.
        n_splits:
            Number of data splits (blocks).
        purge_days:
            Number of days of data to remove between train and test.
        embargo_days:
            Number of days of data to remove after the test block end.
        """
        if not bars or n_splits < 3:
            return CPCVResult()

        n_bars = len(bars)
        block_size = n_bars // n_splits
        if block_size < 2:
            return CPCVResult()

        # Create blocks
        blocks: list[list[OHLCV]] = []
        for i in range(n_splits):
            start = i * block_size
            end = start + block_size if i < n_splits - 1 else n_bars
            blocks.append(bars[start:end])

        # Estimate bars per day for purge/embargo
        total_days = (bars[-1].timestamp - bars[0].timestamp).days
        bars_per_day = max(len(bars) / max(total_days, 1), 1)
        purge_bars = int(purge_days * bars_per_day)
        embargo_bars = int(embargo_days * bars_per_day)

        # Generate all C(n_splits, 2) test combinations
        test_combos = list(itertools.combinations(range(n_splits), 2))

        fold_results: list[FoldResult] = []
        path_sharpes: list[float] = []

        for fold_idx, (test_a, test_b) in enumerate(test_combos):
            test_indices = {test_a, test_b}
            train_indices = [i for i in range(n_splits) if i not in test_indices]

            # Build train data with purging and embargo
            train_bars: list[OHLCV] = []
            for ti in train_indices:
                block = blocks[ti]
                # Purge: remove bars at the boundary near test blocks
                purged = self._apply_purge_embargo(
                    block=block,
                    block_index=ti,
                    test_indices=test_indices,
                    all_blocks=blocks,
                    purge_bars=purge_bars,
                    embargo_bars=embargo_bars,
                )
                train_bars.extend(purged)

            # Build test data (concatenate test blocks)
            test_bars: list[OHLCV] = []
            for ti in sorted(test_indices):
                test_bars.extend(blocks[ti])

            if not train_bars or not test_bars:
                continue

            # Run train backtest
            is_result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=strategy_config,
                bars=train_bars,
                initial_capital=self._initial_capital,
                commission=self._commission,
                symbol=symbol,
                timeframe=timeframe,
            )

            # Run test backtest
            oos_result = await self._runner.run(
                strategy_class=strategy_class,
                strategy_config=strategy_config,
                bars=test_bars,
                initial_capital=self._initial_capital,
                commission=self._commission,
                symbol=symbol,
                timeframe=timeframe,
            )

            fold_results.append(
                FoldResult(
                    fold_index=fold_idx,
                    in_sample_metrics=is_result,
                    out_of_sample_metrics=oos_result,
                )
            )
            path_sharpes.append(oos_result.sharpe_ratio)

        # Compute PBO
        pbo = self._compute_pbo(fold_results)

        # Deflated Sharpe across all paths
        deflated_sharpe = 0.0
        if path_sharpes:
            mean_sharpe = float(np.mean(path_sharpes))
            deflated_sharpe = mean_sharpe  # Simplified; full DSR would use _compute_deflated_sharpe

        return CPCVResult(
            fold_results=fold_results,
            pbo_score=pbo,
            deflated_sharpe=deflated_sharpe,
            path_sharpes=path_sharpes,
        )

    @staticmethod
    def _apply_purge_embargo(
        block: list[OHLCV],
        block_index: int,
        test_indices: set[int],
        all_blocks: list[list[OHLCV]],
        purge_bars: int,
        embargo_bars: int,
    ) -> list[OHLCV]:
        """Remove purge/embargo bars from a training block near test boundaries.

        - If a test block is immediately after this train block:
          remove the last ``purge_bars`` from this block.
        - If a test block is immediately before this train block:
          remove the first ``embargo_bars`` from this block.
        """
        start_trim = 0
        end_trim = len(block)

        # Check if any test block is right after this block
        if (block_index + 1) in test_indices:
            end_trim = max(0, len(block) - purge_bars)

        # Check if any test block is right before this block
        if (block_index - 1) in test_indices:
            start_trim = min(embargo_bars, len(block))

        if start_trim >= end_trim:
            return []
        return block[start_trim:end_trim]

    @staticmethod
    def _compute_pbo(fold_results: list[FoldResult]) -> float:
        """Compute Probability of Backtest Overfitting.

        PBO = fraction of path combinations where the OOS performance
        is worse than the IS performance (rank-based).

        Returns a value between 0 and 1.
        """
        if not fold_results:
            return 0.0

        n_underperform = 0
        n_total = len(fold_results)

        for fold in fold_results:
            is_sharpe = fold.in_sample_metrics.sharpe_ratio
            oos_sharpe = fold.out_of_sample_metrics.sharpe_ratio
            if oos_sharpe < is_sharpe:
                n_underperform += 1

        return n_underperform / n_total if n_total > 0 else 0.0
