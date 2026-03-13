"""Backtest result metrics: trade statistics, risk-adjusted returns, and advanced analytics.

All financial quantities use Decimal. Performance-critical array operations use numpy.
"""

from __future__ import annotations

import decimal
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
from numpy import ndarray

# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Trade:
    """A completed round-trip trade."""

    entry_time: datetime
    exit_time: datetime
    symbol: str
    direction: str  # "LONG" or "SHORT"
    entry_price: Decimal
    exit_price: Decimal
    quantity: Decimal
    pnl: Decimal
    fees: Decimal

    @property
    def duration(self) -> timedelta:
        return self.exit_time - self.entry_time

    @property
    def return_pct(self) -> Decimal:
        if self.entry_price == 0:
            return Decimal("0")
        if self.direction == "LONG":
            return (self.exit_price - self.entry_price) / self.entry_price
        return (self.entry_price - self.exit_price) / self.entry_price


# ---------------------------------------------------------------------------
# BacktestResult
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class BacktestResult:
    """Complete backtest result with all computed metrics."""

    # Return metrics
    total_return: Decimal = Decimal("0")
    annualized_return: Decimal = Decimal("0")

    # Drawdown
    max_drawdown: Decimal = Decimal("0")
    max_drawdown_duration: timedelta = field(default_factory=timedelta)

    # Risk-adjusted
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    calmar_ratio: float = 0.0

    # Trade statistics
    total_trades: int = 0
    win_rate: Decimal = Decimal("0")
    profit_factor: Decimal = Decimal("0")
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    largest_win: Decimal = Decimal("0")
    largest_loss: Decimal = Decimal("0")
    avg_trade_duration: timedelta = field(default_factory=timedelta)

    # Advanced
    deflated_sharpe_ratio: float = 0.0
    probability_of_backtest_overfitting: float = 0.0

    # Series data
    equity_curve: list[Decimal] = field(default_factory=list)
    drawdown_series: list[Decimal] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    monthly_returns: dict[str, Decimal] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Metric computation helpers
# ---------------------------------------------------------------------------


def _decimal_to_float_array(values: list[Decimal]) -> ndarray:
    """Convert a list of Decimal to a numpy float64 array."""
    return np.array([float(v) for v in values], dtype=np.float64)


def _compute_returns(equity: ndarray) -> ndarray:
    """Compute period-over-period returns from an equity curve."""
    if len(equity) < 2:
        return np.array([], dtype=np.float64)
    returns = np.diff(equity) / equity[:-1]
    # Replace inf/nan with 0
    returns = np.where(np.isfinite(returns), returns, 0.0)
    return returns


def _compute_total_return(equity: list[Decimal]) -> Decimal:
    """(final - initial) / initial"""
    if len(equity) < 2 or equity[0] == 0:
        return Decimal("0")
    return (equity[-1] - equity[0]) / equity[0]


def _compute_annualized_return(total_return: Decimal, n_periods: int) -> Decimal:
    """Annualize return assuming daily periods (252 trading days)."""
    if n_periods <= 0:
        return Decimal("0")
    total_r = float(total_return)
    if total_r <= -1.0:
        return Decimal("-1")
    try:
        ann = (1.0 + total_r) ** (252.0 / n_periods) - 1.0
        if not math.isfinite(ann):
            return Decimal(str(round(total_r, 6)))
        return Decimal(str(ann)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    except (OverflowError, decimal.InvalidOperation):
        # Extremely large annualized return; return a capped value
        return Decimal(str(round(total_r, 6)))


def _compute_drawdown_series(equity: ndarray) -> tuple[ndarray, float, int]:
    """Compute drawdown series, max drawdown, and max drawdown duration (in periods).

    Returns (drawdown_array, max_dd_float, max_dd_duration_periods).
    """
    if len(equity) == 0:
        return np.array([], dtype=np.float64), 0.0, 0

    running_max = np.maximum.accumulate(equity)
    # Avoid division by zero
    safe_max = np.where(running_max > 0, running_max, 1.0)
    drawdown = (running_max - equity) / safe_max
    max_dd = float(np.max(drawdown)) if len(drawdown) > 0 else 0.0

    # Max drawdown duration: longest consecutive period below the peak
    max_dur = 0
    current_dur = 0
    for dd in drawdown:
        if dd > 0:
            current_dur += 1
            max_dur = max(max_dur, current_dur)
        else:
            current_dur = 0

    return drawdown, max_dd, max_dur


def _compute_sharpe(returns: ndarray, risk_free_rate: float = 0.0) -> float:
    """Annualized Sharpe ratio (daily returns assumed)."""
    if len(returns) < 2:
        return 0.0
    daily_rf = risk_free_rate / 252.0
    excess = returns - daily_rf
    std = float(np.std(excess, ddof=1))
    if std == 0:
        if float(np.mean(excess)) > 0:
            return float("inf")
        if float(np.mean(excess)) < 0:
            return float("-inf")
        return 0.0
    return float(np.mean(excess)) / std * math.sqrt(252.0)


def _compute_sortino(returns: ndarray, risk_free_rate: float = 0.0) -> float:
    """Annualized Sortino ratio (only downside deviation)."""
    if len(returns) < 2:
        return 0.0
    daily_rf = risk_free_rate / 252.0
    excess = returns - daily_rf
    downside = excess[excess < 0]
    if len(downside) == 0:
        mean_excess = float(np.mean(excess))
        if mean_excess > 0:
            return float("inf")
        return 0.0
    downside_std = float(np.sqrt(np.mean(downside**2)))
    if downside_std == 0:
        return 0.0
    return float(np.mean(excess)) / downside_std * math.sqrt(252.0)


def _compute_calmar(annualized_return: float, max_drawdown: float) -> float:
    """Calmar ratio = annualized return / max drawdown."""
    if max_drawdown == 0:
        return 0.0
    return annualized_return / max_drawdown


def _compute_trade_stats(
    trades: list[Trade],
) -> dict[str, Any]:
    """Compute aggregate trade statistics."""
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": Decimal("0"),
            "profit_factor": Decimal("0"),
            "avg_win": Decimal("0"),
            "avg_loss": Decimal("0"),
            "largest_win": Decimal("0"),
            "largest_loss": Decimal("0"),
            "avg_trade_duration": timedelta(),
        }

    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]
    total = len(trades)

    win_rate = Decimal(str(len(wins))) / Decimal(str(total)) if total > 0 else Decimal("0")

    gross_profit = sum((t.pnl for t in wins), Decimal("0"))
    gross_loss = sum((abs(t.pnl) for t in losses), Decimal("0"))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else Decimal("0")

    avg_win = gross_profit / Decimal(str(len(wins))) if wins else Decimal("0")
    avg_loss = gross_loss / Decimal(str(len(losses))) if losses else Decimal("0")

    largest_win = max((t.pnl for t in wins), default=Decimal("0"))
    largest_loss = min((t.pnl for t in losses), default=Decimal("0"))

    total_duration = sum((t.duration for t in trades), timedelta())
    avg_duration = total_duration / total if total > 0 else timedelta()

    return {
        "total_trades": total,
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "largest_win": largest_win,
        "largest_loss": largest_loss,
        "avg_trade_duration": avg_duration,
    }


def _compute_monthly_returns(
    equity_curve: list[Decimal],
    timestamps: list[datetime] | None = None,
) -> dict[str, Decimal]:
    """Compute monthly return percentages keyed by 'YYYY-MM'."""
    if len(equity_curve) < 2:
        return {}

    if timestamps is None:
        return {}

    monthly: dict[str, Decimal] = {}
    # Group by month
    month_start_equity: dict[str, Decimal] = {}
    month_end_equity: dict[str, Decimal] = {}

    for ts, eq in zip(timestamps, equity_curve, strict=False):
        key = ts.strftime("%Y-%m")
        if key not in month_start_equity:
            month_start_equity[key] = eq
        month_end_equity[key] = eq

    for key in month_start_equity:
        start = month_start_equity[key]
        end = month_end_equity[key]
        if start != 0:
            monthly[key] = (end - start) / start
        else:
            monthly[key] = Decimal("0")

    return monthly


def _compute_deflated_sharpe(
    observed_sharpe: float,
    n_trials: int,
    n_returns: int,
    returns_skew: float,
    returns_kurtosis: float,
) -> float:
    """Bailey & Lopez de Prado Deflated Sharpe Ratio.

    Adjusts the observed Sharpe ratio for the number of strategy trials,
    non-normal return distribution (skewness, kurtosis), and sample length.

    Returns the DSR as a probability (0-1 range, from a one-sided normal CDF).
    """
    from scipy import stats  # type: ignore[import-untyped]

    if n_trials <= 0 or n_returns < 2:
        return 0.0

    # Expected maximum Sharpe under null (Euler-Mascheroni approximation)
    euler_mascheroni = 0.5772156649
    if n_trials == 1:
        e_max_sharpe = 0.0
    else:
        ln_trials = math.log(n_trials)
        e_max_sharpe = math.sqrt(2.0 * ln_trials) - (
            euler_mascheroni / math.sqrt(2.0 * ln_trials)
            + math.log(math.sqrt(math.pi * ln_trials)) / math.sqrt(2.0 * ln_trials)
        )

    # Variance of the Sharpe estimator (Lo, 2002 + higher moments)
    sr = observed_sharpe / math.sqrt(252.0) if observed_sharpe != 0 else 0.0  # de-annualize
    var_sr = (1.0 - returns_skew * sr + ((returns_kurtosis - 1.0) / 4.0) * sr**2) / (
        n_returns - 1.0
    )

    if var_sr <= 0:
        return 0.0

    # Test statistic
    numerator = sr - e_max_sharpe / math.sqrt(252.0)
    denominator = math.sqrt(var_sr)
    if denominator == 0:
        return 0.0

    t_stat = numerator / denominator
    dsr = float(stats.norm.cdf(t_stat))
    return dsr


# ---------------------------------------------------------------------------
# Main calculation function
# ---------------------------------------------------------------------------


def calculate_metrics(
    equity_curve: list[Decimal],
    trades: list[Trade],
    risk_free_rate: float = 0.0,
    timestamps: list[datetime] | None = None,
    n_trials: int = 1,
) -> BacktestResult:
    """Compute all backtest metrics from equity curve and trade list.

    Parameters
    ----------
    equity_curve:
        Time series of portfolio values (one per bar/day).
    trades:
        Completed round-trip trades.
    risk_free_rate:
        Annualized risk-free rate (e.g. 0.05 for 5%).
    timestamps:
        Optional bar timestamps aligned with equity_curve, for monthly returns.
    n_trials:
        Number of strategy configurations tested (for deflated Sharpe).
    """
    equity_arr = _decimal_to_float_array(equity_curve)
    returns = _compute_returns(equity_arr)

    # Basic returns
    total_return = _compute_total_return(equity_curve)
    n_periods = max(len(returns), 1)
    annualized_return = _compute_annualized_return(total_return, n_periods)

    # Drawdown
    dd_series, max_dd, max_dd_dur = _compute_drawdown_series(equity_arr)
    drawdown_list = [Decimal(str(round(d, 8))) for d in dd_series]

    # Risk-adjusted ratios
    sharpe = _compute_sharpe(returns, risk_free_rate)
    sortino = _compute_sortino(returns, risk_free_rate)
    calmar = _compute_calmar(float(annualized_return), max_dd)

    # Trade stats
    tstats = _compute_trade_stats(trades)

    # Monthly returns
    monthly = _compute_monthly_returns(equity_curve, timestamps)

    # Advanced: Deflated Sharpe Ratio
    dsr = 0.0
    if len(returns) >= 2 and n_trials >= 1:
        try:
            ret_std = float(np.std(returns, ddof=1))
            if ret_std > 0:
                z = (returns - np.mean(returns)) / ret_std
                skew = float(np.mean(z**3))
                kurt = float(np.mean(z**4))
            else:
                skew = 0.0
                kurt = 3.0
            dsr = _compute_deflated_sharpe(sharpe, n_trials, len(returns), skew, kurt)
        except Exception:
            dsr = 0.0

    return BacktestResult(
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=Decimal(str(round(max_dd, 8))),
        max_drawdown_duration=timedelta(days=max_dd_dur),
        sharpe_ratio=sharpe,
        sortino_ratio=sortino,
        calmar_ratio=calmar,
        total_trades=tstats["total_trades"],
        win_rate=tstats["win_rate"],
        profit_factor=tstats["profit_factor"],
        avg_win=tstats["avg_win"],
        avg_loss=tstats["avg_loss"],
        largest_win=tstats["largest_win"],
        largest_loss=tstats["largest_loss"],
        avg_trade_duration=tstats["avg_trade_duration"],
        deflated_sharpe_ratio=dsr,
        probability_of_backtest_overfitting=0.0,
        equity_curve=list(equity_curve),
        drawdown_series=drawdown_list,
        trades=list(trades),
        monthly_returns=monthly,
    )
