"""M06: Backtesting engine, walk-forward analysis, CPCV, VectorBT."""

from __future__ import annotations

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

_IMPORT_MAP: dict[str, tuple[str, str]] = {
    "CommissionConfig": ("hydra.backtest.fills", "CommissionConfig"),
    "FillSimulator": ("hydra.backtest.fills", "FillSimulator"),
    "SlippageModel": ("hydra.backtest.fills", "SlippageModel"),
    "BacktestResult": ("hydra.backtest.metrics", "BacktestResult"),
    "Trade": ("hydra.backtest.metrics", "Trade"),
    "calculate_metrics": ("hydra.backtest.metrics", "calculate_metrics"),
    "BacktestRunner": ("hydra.backtest.runner", "BacktestRunner"),
    "SweepResult": ("hydra.backtest.vectorbt_research", "SweepResult"),
    "VectorBTResearch": ("hydra.backtest.vectorbt_research", "VectorBTResearch"),
    "CPCVResult": ("hydra.backtest.walkforward", "CPCVResult"),
    "FoldResult": ("hydra.backtest.walkforward", "FoldResult"),
    "WalkForwardAnalyzer": ("hydra.backtest.walkforward", "WalkForwardAnalyzer"),
    "WalkForwardResult": ("hydra.backtest.walkforward", "WalkForwardResult"),
}


def __getattr__(name: str) -> object:
    if name in _IMPORT_MAP:
        module_path, attr = _IMPORT_MAP[name]
        import importlib

        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
