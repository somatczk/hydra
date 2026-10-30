"""Backtest result metrics: trade statistics, risk-adjusted returns and the buy-and-hold benchmark.

All financial quantities use Decimal; array maths uses numpy float64. This docstring is the one
place the definitions live.

Conventions: crypto trades 24/7, so a year is 365 days and periods per year
``ppy = 365 * 86400 / timeframe_seconds`` (8760 for 1h, 365 for 1d). ``e_0..e_n`` is the equity
curve, ``r_i = e_i / e_{i-1} - 1`` the per-bar simple returns (N = n of them), rf = 0.
Annualisation counts bars, so data gaps (provenance ``gap_count``) are not calendar-corrected.

================== ============================================================ ================
Metric             Definition                                                   Undefined case
================== ============================================================ ================
total_return       e_n / e_0 - 1 (fraction)                                     0 if < 2 points
annualized_return  (1 + total_return) ** (ppy / N) - 1, floored at -1           0 if N = 0;
                                                                                None on overflow
max_drawdown       max_i (max_{j<=i} e_j - e_i) / max_{j<=i} e_j (fraction)     0
max_dd_duration    longest run of bars below the running peak * bar length      0
sharpe_ratio       mean(r) / std(r, ddof=1) * sqrt(ppy)                         0.0 if std<1e-12
sortino_ratio      mean(r) / sqrt(mean(min(r, 0) ** 2)) * sqrt(ppy), all N      None if no loss
calmar_ratio       annualized_return / max_drawdown                             None if dd = 0
                                                                                or ann. None
win_rate           wins / trades; a break-even trade is not a win               0
profit_factor      sum(winning pnl) / |sum(losing pnl)|                         None if no loss
expectancy         mean trade pnl (= win_rate*avg_win - loss_rate*avg_loss)     0
benchmark_equity   initial_capital * p_i / p_0 on ``benchmark_prices``, which   [] if no prices
                   are aligned 1:1 with the equity curve (frictionless)
benchmark_sharpe   the Sharpe formula on the benchmark equity's returns r_b      0.0
beta               cov(r, r_b, ddof=1) / var(r_b, ddof=1)                       0.0
alpha              Jensen, annualised: (mean(r) - beta * mean(r_b)) * ppy       0.0
================== ============================================================ ================

``Trade.pnl`` is net of both fees: the executor's ``net_pnl`` takes the exit fee and the pro-rata
entry fee. The API reports win_rate, max_drawdown and returns in percent on /backtest results
and win_rate as a fraction on /strategies/preview.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.core.types import Timeframe
from hydra.data.bars import timeframe_seconds

# Type alias for individual fill transactions (entry/exit, linked by trade_id)
TransactionRecord = dict[str, Any]

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
    annualized_return: Decimal | None = Decimal("0")

    # Drawdown
    max_drawdown: Decimal = Decimal("0")
    max_drawdown_duration: timedelta = field(default_factory=timedelta)

    # Risk-adjusted
    sharpe_ratio: float = 0.0
    sortino_ratio: float | None = None
    calmar_ratio: float | None = None

    # Trade statistics
    total_trades: int = 0
    win_rate: Decimal = Decimal("0")
    profit_factor: Decimal | None = None
    avg_win: Decimal = Decimal("0")
    avg_loss: Decimal = Decimal("0")
    largest_win: Decimal = Decimal("0")
    largest_loss: Decimal = Decimal("0")
    avg_trade_duration: timedelta = field(default_factory=timedelta)

    # Benchmark (buy-and-hold)
    benchmark_equity: list[Decimal] = field(default_factory=list)
    benchmark_return: Decimal = Decimal("0")
    benchmark_sharpe: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0

    # Streak / expectancy
    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0
    expectancy: Decimal = Decimal("0")

    # Series data
    equity_curve: list[Decimal] = field(default_factory=list)
    timestamps: list[datetime] = field(default_factory=list)
    drawdown_series: list[Decimal] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    monthly_returns: dict[str, Decimal] = field(default_factory=dict)

    # Individual fill transactions (entry/exit, linked by trade_id)
    transactions: list[TransactionRecord] = field(default_factory=list)

    # Early termination
    stopped_reason: str | None = None


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


def _compute_annualized_return(total_return: Decimal, n_returns: int, ppy: float) -> Decimal | None:
    """Compound growth over ``n_returns`` bars, scaled to ``ppy`` bars per year."""
    if n_returns <= 0:
        return Decimal("0")
    total_r = float(total_return)
    if total_r <= -1.0:
        return Decimal("-1")
    try:
        ann = (1.0 + total_r) ** (ppy / n_returns) - 1.0
    except OverflowError:
        return None
    return Decimal(str(round(ann, 6)))


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


_DEGENERATE = 1e-12


def _compute_sharpe(returns: ndarray, ppy: float) -> float:
    """Annualised Sharpe ratio; 0.0 when the dispersion is float noise."""
    if len(returns) < 2:
        return 0.0
    std = float(np.std(returns, ddof=1))
    if std < _DEGENERATE:
        return 0.0
    return float(np.mean(returns)) / std * math.sqrt(ppy)


def _compute_sortino(returns: ndarray, ppy: float) -> float | None:
    """Annualised Sortino ratio with the target downside deviation over all returns."""
    if len(returns) < 2:
        return None
    tdd = float(np.sqrt(np.mean(np.minimum(returns, 0.0) ** 2)))
    if tdd < _DEGENERATE:
        return None
    return float(np.mean(returns)) / tdd * math.sqrt(ppy)


def _compute_calmar(annualized_return: Decimal | None, max_drawdown: float) -> float | None:
    """Calmar ratio = annualized return / max drawdown."""
    if annualized_return is None or max_drawdown == 0:
        return None
    return float(annualized_return) / max_drawdown


def _compute_trade_stats(
    trades: list[Trade],
) -> dict[str, Any]:
    """Compute aggregate trade statistics."""
    if not trades:
        return {
            "total_trades": 0,
            "win_rate": Decimal("0"),
            "profit_factor": None,
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
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None

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


def _compute_benchmark_equity(
    initial_capital: Decimal,
    benchmark_prices: list[Decimal],
) -> list[Decimal]:
    """Buy-and-hold equity: initial_capital * price / first price."""
    if not benchmark_prices or benchmark_prices[0] == 0:
        return []
    first_price = benchmark_prices[0]
    return [initial_capital * (price / first_price) for price in benchmark_prices]


def _compute_alpha_beta(
    strategy_returns: ndarray,
    benchmark_returns: ndarray,
    ppy: float,
) -> tuple[float, float]:
    """Beta and annualised Jensen alpha of equal-length per-bar return series."""
    if len(strategy_returns) < 2 or len(strategy_returns) != len(benchmark_returns):
        return 0.0, 0.0
    var_b = float(np.var(benchmark_returns, ddof=1))
    if var_b == 0:
        return 0.0, 0.0
    beta = float(np.cov(strategy_returns, benchmark_returns, ddof=1)[0][1]) / var_b
    alpha = (float(np.mean(strategy_returns)) - beta * float(np.mean(benchmark_returns))) * ppy
    return alpha, beta


def _compute_consecutive_wins_losses(trades: list[Trade]) -> tuple[int, int]:
    """Compute max consecutive wins and max consecutive losses from trade list."""
    if not trades:
        return 0, 0

    max_wins = 0
    max_losses = 0
    current_wins = 0
    current_losses = 0

    for trade in trades:
        if trade.pnl > 0:
            current_wins += 1
            current_losses = 0
            max_wins = max(max_wins, current_wins)
        elif trade.pnl < 0:
            current_losses += 1
            current_wins = 0
            max_losses = max(max_losses, current_losses)
        else:
            # Breakeven trade resets both streaks
            current_wins = 0
            current_losses = 0

    return max_wins, max_losses


def _compute_expectancy(trades: list[Trade]) -> Decimal:
    """Compute expectancy: (win_rate * avg_win) - (loss_rate * avg_loss).

    Returns the expected value per trade.
    """
    if not trades:
        return Decimal("0")
    total = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl < 0]

    win_rate = Decimal(str(len(wins))) / Decimal(str(total))
    loss_rate = Decimal(str(len(losses))) / Decimal(str(total))

    avg_win = (
        sum((t.pnl for t in wins), Decimal("0")) / Decimal(str(len(wins))) if wins else Decimal("0")
    )
    avg_loss = (
        sum((abs(t.pnl) for t in losses), Decimal("0")) / Decimal(str(len(losses)))
        if losses
        else Decimal("0")
    )

    return win_rate * avg_win - loss_rate * avg_loss


# ---------------------------------------------------------------------------
# Main calculation function
# ---------------------------------------------------------------------------


def calculate_metrics(
    equity_curve: list[Decimal],
    trades: list[Trade],
    *,
    timeframe: Timeframe,
    timestamps: list[datetime] | None = None,
    benchmark_prices: list[Decimal] | None = None,
    initial_capital: Decimal = Decimal("100000"),
) -> BacktestResult:
    """Compute all backtest metrics (definitions in the module docstring).

    ``timestamps`` and ``benchmark_prices``, when given, are aligned 1:1 with ``equity_curve``.
    """
    for name, series in (("timestamps", timestamps), ("benchmark_prices", benchmark_prices)):
        if series is not None and len(series) != len(equity_curve):
            msg = f"{name} has {len(series)} points, equity_curve has {len(equity_curve)}"
            raise ValueError(msg)
    tf_seconds = timeframe_seconds(timeframe)
    ppy = 365 * 86400 / tf_seconds
    equity_arr = _decimal_to_float_array(equity_curve)
    returns = _compute_returns(equity_arr)

    total_return = _compute_total_return(equity_curve)
    annualized_return = _compute_annualized_return(total_return, len(returns), ppy)

    dd_series, max_dd, max_dd_dur = _compute_drawdown_series(equity_arr)
    drawdown_list = [Decimal(str(round(d, 8))) for d in dd_series]

    sharpe = _compute_sharpe(returns, ppy)
    sortino = _compute_sortino(returns, ppy)
    calmar = _compute_calmar(annualized_return, max_dd)

    tstats = _compute_trade_stats(trades)
    monthly = _compute_monthly_returns(equity_curve, timestamps)

    # Benchmark (buy-and-hold)
    benchmark_equity: list[Decimal] = []
    benchmark_return = Decimal("0")
    benchmark_sharpe = 0.0
    alpha = 0.0
    beta = 0.0
    if benchmark_prices:
        benchmark_equity = _compute_benchmark_equity(initial_capital, benchmark_prices)
        benchmark_return = _compute_total_return(benchmark_equity)
        benchmark_returns = _compute_returns(_decimal_to_float_array(benchmark_equity))
        benchmark_sharpe = _compute_sharpe(benchmark_returns, ppy)
        alpha, beta = _compute_alpha_beta(returns, benchmark_returns, ppy)

    # Consecutive wins/losses and expectancy
    max_consecutive_wins, max_consecutive_losses = _compute_consecutive_wins_losses(trades)
    expectancy = _compute_expectancy(trades)

    return BacktestResult(
        total_return=total_return,
        annualized_return=annualized_return,
        max_drawdown=Decimal(str(round(max_dd, 8))),
        max_drawdown_duration=timedelta(seconds=max_dd_dur * tf_seconds),
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
        benchmark_equity=benchmark_equity,
        benchmark_return=benchmark_return,
        benchmark_sharpe=benchmark_sharpe,
        alpha=alpha,
        beta=beta,
        max_consecutive_wins=max_consecutive_wins,
        max_consecutive_losses=max_consecutive_losses,
        expectancy=expectancy,
        equity_curve=list(equity_curve),
        timestamps=list(timestamps or []),
        drawdown_series=drawdown_list,
        trades=list(trades),
        monthly_returns=monthly,
    )
