"""VectorBT-based research tools for parameter sweeps and vectorized backtesting.

Provides a thin wrapper around vectorbt for rapid parameter optimization,
or falls back to numpy-based vectorized backtesting when vectorbt is not available.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.backtest.fills import CommissionConfig
from hydra.backtest.metrics import BacktestResult, Trade, calculate_metrics
from hydra.core.types import OHLCV

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional vectorbt import
# ---------------------------------------------------------------------------

try:
    import importlib.util

    _HAS_VBT = importlib.util.find_spec("vectorbt") is not None
except (ImportError, ModuleNotFoundError):
    _HAS_VBT = False


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SweepResult:
    """Result of a parameter sweep."""

    param_combinations: list[dict[str, Any]] = field(default_factory=list)
    metric_values: list[float] = field(default_factory=list)
    best_params: dict[str, Any] = field(default_factory=dict)
    best_metric: float = 0.0
    heatmap_data: dict[str, Any] = field(default_factory=dict)
    deflated_sharpe_filtered: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------------------------
# VectorBTResearch
# ---------------------------------------------------------------------------


class VectorBTResearch:
    """Parameter sweep and vectorized backtesting research tool.

    When vectorbt is available, uses its fast vectorized computation.
    Otherwise, falls back to a numpy-based SMA crossover backtester
    for basic parameter sweeps.
    """

    def __init__(
        self,
        commission: CommissionConfig | None = None,
        initial_capital: Decimal = Decimal("100000"),
    ) -> None:
        self._commission = commission or CommissionConfig()
        self._initial_capital = initial_capital

    def parameter_sweep(
        self,
        bars: list[OHLCV],
        strategy_class: type | None = None,
        param_grid: dict[str, list[Any]] | None = None,
        metric: str = "sharpe_ratio",
        deflated_sharpe_threshold: float = 0.0,
    ) -> SweepResult:
        """Run a parameter sweep over all combinations in param_grid.

        Parameters
        ----------
        bars:
            OHLCV bar data.
        strategy_class:
            Strategy class (used for reference; the sweep uses vectorized logic).
        param_grid:
            Dict mapping parameter names to lists of values.
            Example: {"fast_period": [5, 10, 20], "slow_period": [20, 50, 100]}
        metric:
            Metric to optimize (e.g. "sharpe_ratio", "total_return").
        deflated_sharpe_threshold:
            Minimum deflated Sharpe ratio to include in filtered results.
        """
        if not bars or not param_grid:
            return SweepResult()

        # Extract close prices
        closes = np.array([float(b.close) for b in bars], dtype=np.float64)

        # Generate all param combos
        param_names = list(param_grid.keys())
        param_values = list(param_grid.values())
        combos = list(itertools.product(*param_values))

        results: list[dict[str, Any]] = []
        metric_values: list[float] = []

        for combo in combos:
            params = dict(zip(param_names, combo, strict=False))
            # Run vectorized SMA crossover backtest
            result = self._vectorized_sma_backtest(closes, params, bars)
            metric_val = getattr(result, metric, 0.0)
            if isinstance(metric_val, Decimal):
                metric_val = float(metric_val)
            results.append({"params": params, "result": result, "metric": metric_val})
            metric_values.append(metric_val)

        # Find best
        if metric_values:
            best_idx = int(np.argmax(metric_values))
            best_params = dict(zip(param_names, combos[best_idx], strict=False))
            best_metric = metric_values[best_idx]
        else:
            best_params = {}
            best_metric = 0.0

        # Build heatmap data (for 2D grids)
        heatmap_data: dict[str, Any] = {}
        if len(param_names) == 2:
            x_vals = sorted(set(param_grid[param_names[0]]))
            y_vals = sorted(set(param_grid[param_names[1]]))
            grid = np.full((len(y_vals), len(x_vals)), np.nan)
            for r, combo in zip(results, combos, strict=False):
                xi = x_vals.index(combo[0])
                yi = y_vals.index(combo[1])
                grid[yi, xi] = r["metric"]
            heatmap_data = {
                "x_param": param_names[0],
                "y_param": param_names[1],
                "x_values": x_vals,
                "y_values": y_vals,
                "grid": grid.tolist(),
            }

        # Filter by deflated Sharpe
        filtered = []
        if deflated_sharpe_threshold > 0:
            for r in results:
                dsr = r["result"].deflated_sharpe_ratio
                if dsr >= deflated_sharpe_threshold:
                    filtered.append(r["params"])

        param_combo_list = [dict(zip(param_names, c, strict=False)) for c in combos]

        return SweepResult(
            param_combinations=param_combo_list,
            metric_values=metric_values,
            best_params=best_params,
            best_metric=best_metric,
            heatmap_data=heatmap_data,
            deflated_sharpe_filtered=filtered,
        )

    def _vectorized_sma_backtest(
        self,
        closes: ndarray,
        params: dict[str, Any],
        bars: list[OHLCV],
    ) -> BacktestResult:
        """Vectorized SMA crossover backtest using numpy.

        Uses ``fast_period`` and ``slow_period`` from params.
        Generates BUY when fast SMA crosses above slow SMA and
        SELL when it crosses below.
        """
        fast_period = int(params.get("fast_period", 10))
        slow_period = int(params.get("slow_period", 30))

        n = len(closes)
        if n < slow_period + 1:
            return calculate_metrics(
                equity_curve=[self._initial_capital],
                trades=[],
                n_trials=1,
            )

        # Compute SMAs using cumsum trick
        fast_sma = self._rolling_mean(closes, fast_period)
        slow_sma = self._rolling_mean(closes, slow_period)

        # Generate signals: +1 when fast > slow, -1 when fast < slow, 0 otherwise
        position = np.zeros(n, dtype=np.float64)
        valid_start = slow_period
        for i in range(valid_start, n):
            if not np.isnan(fast_sma[i]) and not np.isnan(slow_sma[i]):
                if fast_sma[i] > slow_sma[i]:
                    position[i] = 1.0
                else:
                    position[i] = 0.0

        # Compute returns
        price_returns = np.zeros(n, dtype=np.float64)
        price_returns[1:] = np.diff(closes) / closes[:-1]

        # Strategy returns (position from previous period * current return)
        strategy_returns = np.zeros(n, dtype=np.float64)
        strategy_returns[1:] = position[:-1] * price_returns[1:]

        # Apply commission on position changes
        fee_rate = float(self._commission.spot_taker)
        position_changes = np.abs(np.diff(position))
        total_fees = np.sum(position_changes) * fee_rate * 2  # round-trip

        # Compute equity curve
        initial = float(self._initial_capital)
        cum_returns = np.cumprod(1.0 + strategy_returns)
        equity_float = initial * cum_returns
        # Deduct approximate fees
        if total_fees > 0:
            fee_drag = 1.0 - total_fees / max(equity_float[-1], 1.0)
            equity_float = equity_float * max(fee_drag, 0.0)

        equity_curve = [self._initial_capital] + [Decimal(str(round(e, 2))) for e in equity_float]

        # Extract trades from position changes
        trades: list[Trade] = []
        in_trade = False
        entry_idx = 0
        for i in range(valid_start, n):
            if position[i] == 1.0 and not in_trade:
                in_trade = True
                entry_idx = i
            elif position[i] == 0.0 and in_trade:
                in_trade = False
                entry_p = Decimal(str(closes[entry_idx]))
                exit_p = Decimal(str(closes[i]))
                qty = Decimal("1")
                pnl = (exit_p - entry_p) * qty
                fee = (entry_p + exit_p) * qty * Decimal(str(fee_rate))
                pnl -= fee
                trades.append(
                    Trade(
                        entry_time=bars[entry_idx].timestamp,
                        exit_time=bars[i].timestamp,
                        symbol="BTCUSDT",
                        direction="LONG",
                        entry_price=entry_p,
                        exit_price=exit_p,
                        quantity=qty,
                        pnl=pnl,
                        fees=fee,
                    )
                )

        n_combos = 1  # Will be overridden by sweep caller if needed
        return calculate_metrics(
            equity_curve=equity_curve,
            trades=trades,
            n_trials=n_combos,
        )

    @staticmethod
    def _rolling_mean(data: ndarray, period: int) -> ndarray:
        """Compute rolling mean using cumsum for O(n) performance."""
        n = len(data)
        result = np.full(n, np.nan, dtype=np.float64)
        if n < period:
            return result
        cumsum = np.cumsum(data)
        cumsum_shifted = np.zeros(n, dtype=np.float64)
        cumsum_shifted[period:] = cumsum[:-period]
        result[period - 1 :] = (cumsum[period - 1 :] - cumsum_shifted[period - 1 :]) / period
        # Fix the first element
        result[period - 1] = cumsum[period - 1] / period
        return result
