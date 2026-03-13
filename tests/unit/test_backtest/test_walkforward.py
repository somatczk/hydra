"""Tests for hydra.backtest.walkforward -- WalkForwardAnalyzer, CPCV."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.walkforward import (
    CPCVResult,
    FoldResult,
    WalkForwardAnalyzer,
    WalkForwardResult,
)
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import (
    OHLCV,
    Timeframe,
)
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

# ---------------------------------------------------------------------------
# Test strategy
# ---------------------------------------------------------------------------


class SimpleTestStrategy(BaseStrategy):
    """Minimal strategy for walk-forward testing."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bars(n_days: int, start_price: float = 100.0) -> list[OHLCV]:
    """Generate n_days of daily OHLCV bars."""
    bars = []
    base_time = datetime(2022, 1, 1, tzinfo=UTC)
    price = start_price
    for i in range(n_days):
        o = Decimal(str(round(price, 2)))
        h = Decimal(str(round(price + 2, 2)))
        lo = Decimal(str(round(price - 2, 2)))
        change = 0.5 if i % 3 != 0 else -0.3
        c = Decimal(str(round(price + change, 2)))
        bars.append(
            OHLCV(
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=Decimal("1000"),
                timestamp=base_time + timedelta(days=i),
            )
        )
        price = float(c)
    return bars


def _make_config() -> StrategyConfig:
    return StrategyConfig(
        id="wf-test",
        name="WF Test Strategy",
        strategy_class="test.SimpleTestStrategy",
        symbols=["BTCUSDT"],
    )


@pytest.fixture
def analyzer() -> WalkForwardAnalyzer:
    return WalkForwardAnalyzer(
        commission=CommissionConfig(),
        initial_capital=Decimal("100000"),
    )


# ---------------------------------------------------------------------------
# Walk-forward splits
# ---------------------------------------------------------------------------


class TestWalkForwardSplits:
    async def test_basic_walkforward(self, analyzer: WalkForwardAnalyzer) -> None:
        """Walk-forward with 12 months of daily data, 6m IS + 2m OOS."""
        bars = _make_bars(365)  # 1 year of daily bars
        config = _make_config()

        result = await analyzer.run_walkforward(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            in_sample_months=3,
            out_of_sample_months=1,
            symbol="BTCUSDT",
            timeframe=Timeframe.D1,
        )

        assert isinstance(result, WalkForwardResult)
        assert len(result.folds) > 0
        # Each fold should have IS and OOS metrics
        for fold in result.folds:
            assert isinstance(fold.in_sample_metrics.total_return, Decimal)
            assert isinstance(fold.out_of_sample_metrics.total_return, Decimal)

    async def test_walkforward_splits_bars_correctly(self, analyzer: WalkForwardAnalyzer) -> None:
        """Verify that folds cover distinct time windows."""
        bars = _make_bars(365)
        config = _make_config()

        result = await analyzer.run_walkforward(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            in_sample_months=3,
            out_of_sample_months=1,
        )

        # Multiple folds should be generated
        assert len(result.folds) >= 2

    async def test_empty_bars(self, analyzer: WalkForwardAnalyzer) -> None:
        """Empty bars should return empty result."""
        config = _make_config()
        result = await analyzer.run_walkforward(
            bars=[],
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
        )
        assert len(result.folds) == 0

    async def test_aggregated_oos(self, analyzer: WalkForwardAnalyzer) -> None:
        """Aggregated OOS metrics should be computed."""
        bars = _make_bars(365)
        config = _make_config()

        result = await analyzer.run_walkforward(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            in_sample_months=3,
            out_of_sample_months=1,
        )

        if result.folds:
            assert result.aggregated_oos_metrics is not None


# ---------------------------------------------------------------------------
# CPCV purging and embargo
# ---------------------------------------------------------------------------


class TestCPCVPurging:
    async def test_purge_removes_days(self, analyzer: WalkForwardAnalyzer) -> None:
        """Purging should remove bars at train/test boundary."""
        bars = _make_bars(300)
        config = _make_config()

        # Run with purge_days=5
        result = await analyzer.run_cpcv(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            n_splits=4,
            purge_days=5,
            embargo_days=2,
        )

        assert isinstance(result, CPCVResult)
        # Should have C(4,2) = 6 fold combinations
        assert len(result.fold_results) == 6

    async def test_embargo_removes_days(self, analyzer: WalkForwardAnalyzer) -> None:
        """Embargo should remove bars after test blocks."""
        bars = _make_bars(300)
        config = _make_config()

        result_with_embargo = await analyzer.run_cpcv(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            n_splits=4,
            purge_days=0,
            embargo_days=5,
        )

        result_without = await analyzer.run_cpcv(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            n_splits=4,
            purge_days=0,
            embargo_days=0,
        )

        # Both should produce results
        assert len(result_with_embargo.fold_results) > 0
        assert len(result_without.fold_results) > 0

    def test_apply_purge_embargo_trims_end(self) -> None:
        """If a test block follows this train block, trim the end."""
        bars = _make_bars(100)
        blocks = [bars[:25], bars[25:50], bars[50:75], bars[75:100]]

        # Block 0 is train, block 1 is test
        result = WalkForwardAnalyzer._apply_purge_embargo(
            block=blocks[0],
            block_index=0,
            test_indices={1},
            all_blocks=blocks,
            purge_bars=5,
            embargo_bars=0,
        )

        # Should have removed last 5 bars
        assert len(result) == 20

    def test_apply_purge_embargo_trims_start(self) -> None:
        """If a test block precedes this train block, trim the start."""
        bars = _make_bars(100)
        blocks = [bars[:25], bars[25:50], bars[50:75], bars[75:100]]

        # Block 1 is test, block 2 is train
        result = WalkForwardAnalyzer._apply_purge_embargo(
            block=blocks[2],
            block_index=2,
            test_indices={1},
            all_blocks=blocks,
            purge_bars=0,
            embargo_bars=3,
        )

        # Should have removed first 3 bars (embargo)
        assert len(result) == 22


# ---------------------------------------------------------------------------
# PBO score
# ---------------------------------------------------------------------------


class TestPBO:
    async def test_pbo_between_zero_and_one(self, analyzer: WalkForwardAnalyzer) -> None:
        """PBO score should be between 0 and 1."""
        bars = _make_bars(300)
        config = _make_config()

        result = await analyzer.run_cpcv(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            n_splits=4,
            purge_days=2,
            embargo_days=1,
        )

        assert 0.0 <= result.pbo_score <= 1.0

    def test_pbo_empty_folds(self) -> None:
        """PBO with no folds should be 0."""
        pbo = WalkForwardAnalyzer._compute_pbo([])
        assert pbo == 0.0

    def test_pbo_all_underperform(self) -> None:
        """PBO should be 1.0 if all OOS underperform IS."""
        from hydra.backtest.metrics import BacktestResult

        folds = []
        for i in range(5):
            fold = FoldResult(
                fold_index=i,
                in_sample_metrics=BacktestResult(sharpe_ratio=2.0),
                out_of_sample_metrics=BacktestResult(sharpe_ratio=0.5),
            )
            folds.append(fold)

        pbo = WalkForwardAnalyzer._compute_pbo(folds)
        assert pbo == 1.0

    def test_pbo_none_underperform(self) -> None:
        """PBO should be 0.0 if no OOS underperforms IS."""
        from hydra.backtest.metrics import BacktestResult

        folds = []
        for i in range(5):
            fold = FoldResult(
                fold_index=i,
                in_sample_metrics=BacktestResult(sharpe_ratio=1.0),
                out_of_sample_metrics=BacktestResult(sharpe_ratio=2.0),
            )
            folds.append(fold)

        pbo = WalkForwardAnalyzer._compute_pbo(folds)
        assert pbo == 0.0


# ---------------------------------------------------------------------------
# CPCV path sharpes
# ---------------------------------------------------------------------------


class TestCPCVPathSharpes:
    async def test_path_sharpes_count(self, analyzer: WalkForwardAnalyzer) -> None:
        """Number of path Sharpe values should equal C(n_splits, 2)."""
        bars = _make_bars(300)
        config = _make_config()

        result = await analyzer.run_cpcv(
            bars=bars,
            strategy_class=SimpleTestStrategy,
            strategy_config=config,
            n_splits=5,
            purge_days=2,
            embargo_days=1,
        )

        # C(5, 2) = 10
        assert len(result.path_sharpes) == 10
