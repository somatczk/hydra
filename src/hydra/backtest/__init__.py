"""M06: Backtesting engine, walk-forward analysis, CPCV, VectorBT."""

from __future__ import annotations

from hydra.backtest.fills import CommissionConfig, FillSimulator, SlippageModel
from hydra.backtest.metrics import BacktestResult, Trade, calculate_metrics
from hydra.backtest.runner import BacktestRunner
from hydra.backtest.vectorbt_research import SweepResult, VectorBTResearch
from hydra.backtest.walkforward import (
    CPCVResult,
    FoldResult,
    WalkForwardAnalyzer,
    WalkForwardResult,
)

__all__ = [
    "BacktestResult",
    "BacktestRunner",
    "CPCVResult",
    "CommissionConfig",
    "FillSimulator",
    "FoldResult",
    "SlippageModel",
    "SweepResult",
    "Trade",
    "VectorBTResearch",
    "WalkForwardAnalyzer",
    "WalkForwardResult",
    "calculate_metrics",
]
