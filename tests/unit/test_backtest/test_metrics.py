"""Tests for hydra.backtest.metrics -- BacktestResult, Trade, calculate_metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import numpy as np
import pytest

from hydra.backtest.metrics import (
    BacktestResult,
    Trade,
    _compute_alpha_beta,
    _compute_benchmark_equity,
    _compute_consecutive_wins_losses,
    _compute_drawdown_series,
    _compute_expectancy,
    _compute_sharpe,
    _compute_sortino,
    _compute_total_return,
    calculate_metrics,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

T0 = datetime(2024, 1, 1, tzinfo=UTC)
T1 = datetime(2024, 1, 2, tzinfo=UTC)
T2 = datetime(2024, 1, 3, tzinfo=UTC)
T3 = datetime(2024, 1, 4, tzinfo=UTC)
T4 = datetime(2024, 1, 5, tzinfo=UTC)


def _trade(
    pnl: str,
    entry_time: datetime = T0,
    exit_time: datetime = T1,
) -> Trade:
    return Trade(
        entry_time=entry_time,
        exit_time=exit_time,
        symbol="BTCUSDT",
        direction="LONG",
        entry_price=Decimal("100"),
        exit_price=Decimal("100") + Decimal(pnl),
        quantity=Decimal("1"),
        pnl=Decimal(pnl),
        fees=Decimal("0"),
    )


# ---------------------------------------------------------------------------
# Total return
# ---------------------------------------------------------------------------


class TestTotalReturn:
    def test_simple_gain(self) -> None:
        equity = [Decimal("100"), Decimal("110")]
        result = _compute_total_return(equity)
        assert result == Decimal("0.1")

    def test_simple_loss(self) -> None:
        equity = [Decimal("100"), Decimal("80")]
        result = _compute_total_return(equity)
        assert result == Decimal("-0.2")

    def test_no_change(self) -> None:
        equity = [Decimal("100"), Decimal("100")]
        result = _compute_total_return(equity)
        assert result == Decimal("0")

    def test_empty_curve(self) -> None:
        result = _compute_total_return([])
        assert result == Decimal("0")

    def test_single_point(self) -> None:
        result = _compute_total_return([Decimal("100")])
        assert result == Decimal("0")

    def test_calculate_metrics_total_return(self) -> None:
        """Full metrics calculation with known equity curve."""
        equity = [
            Decimal("100"),
            Decimal("105"),
            Decimal("110"),
            Decimal("120"),
        ]
        result = calculate_metrics(equity_curve=equity, trades=[])
        assert result.total_return == Decimal("0.2")


# ---------------------------------------------------------------------------
# Max drawdown
# ---------------------------------------------------------------------------


class TestMaxDrawdown:
    def test_known_drawdown(self) -> None:
        """[100, 110, 90, 95] -> max DD from 110 to 90 = 20/110 ~ 18.18%."""
        equity = np.array([100.0, 110.0, 90.0, 95.0])
        _dd_series, max_dd, _ = _compute_drawdown_series(equity)

        # Max drawdown = (110 - 90) / 110 = 0.18181...
        assert abs(max_dd - 0.181818) < 0.001

    def test_no_drawdown(self) -> None:
        """Monotonically increasing equity has 0 drawdown."""
        equity = np.array([100.0, 110.0, 120.0, 130.0])
        _, max_dd, _ = _compute_drawdown_series(equity)
        assert max_dd == 0.0

    def test_drawdown_duration(self) -> None:
        """[100, 110, 90, 95, 80, 115] -> duration from bar 2 to bar 5 = 4 bars."""
        equity = np.array([100.0, 110.0, 90.0, 95.0, 80.0, 115.0])
        _, _, max_dur = _compute_drawdown_series(equity)
        # Bars 2, 3, 4 are in drawdown (3 periods), then bar 5 recovers above peak
        assert max_dur >= 3

    def test_empty_equity(self) -> None:
        equity = np.array([], dtype=np.float64)
        _, max_dd, _ = _compute_drawdown_series(equity)
        assert max_dd == 0.0

    def test_calculate_metrics_max_drawdown(self) -> None:
        equity = [
            Decimal("100"),
            Decimal("110"),
            Decimal("90"),
            Decimal("95"),
        ]
        result = calculate_metrics(equity_curve=equity, trades=[])
        assert float(result.max_drawdown) == pytest.approx(0.181818, abs=0.001)


# ---------------------------------------------------------------------------
# Sharpe ratio
# ---------------------------------------------------------------------------


class TestSharpeRatio:
    def test_constant_positive_returns(self) -> None:
        """Constant positive returns -> effectively infinite Sharpe."""
        returns = np.array([0.01, 0.01, 0.01, 0.01, 0.01], dtype=np.float64)
        sharpe = _compute_sharpe(returns, risk_free_rate=0.0)
        assert sharpe == float("inf")

    def test_zero_returns(self) -> None:
        """Zero returns -> Sharpe = 0."""
        returns = np.array([0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
        sharpe = _compute_sharpe(returns, risk_free_rate=0.0)
        assert sharpe == 0.0

    def test_positive_sharpe(self) -> None:
        """Mix of positive and negative returns with net positive -> positive Sharpe."""
        returns = np.array([0.02, -0.01, 0.03, -0.005, 0.01], dtype=np.float64)
        sharpe = _compute_sharpe(returns, risk_free_rate=0.0)
        assert sharpe > 0

    def test_negative_sharpe(self) -> None:
        """Net negative returns -> negative Sharpe."""
        returns = np.array([-0.02, -0.01, 0.005, -0.03, -0.01], dtype=np.float64)
        sharpe = _compute_sharpe(returns, risk_free_rate=0.0)
        assert sharpe < 0


# ---------------------------------------------------------------------------
# Sortino ratio
# ---------------------------------------------------------------------------


class TestSortinoRatio:
    def test_only_positive_returns(self) -> None:
        """All positive returns -> infinite Sortino (no downside deviation)."""
        returns = np.array([0.01, 0.02, 0.015, 0.005, 0.03], dtype=np.float64)
        sortino = _compute_sortino(returns, risk_free_rate=0.0)
        assert sortino == float("inf")

    def test_only_penalizes_downside(self) -> None:
        """Sortino should be higher than Sharpe when upside volatility is large."""
        # Large upside swings, small downside
        returns = np.array([0.05, -0.001, 0.08, -0.002, 0.1], dtype=np.float64)
        sharpe = _compute_sharpe(returns, risk_free_rate=0.0)
        sortino = _compute_sortino(returns, risk_free_rate=0.0)
        # Sortino should be higher because upside vol doesn't penalize
        assert sortino > sharpe

    def test_zero_returns(self) -> None:
        returns = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        sortino = _compute_sortino(returns, risk_free_rate=0.0)
        assert sortino == 0.0


# ---------------------------------------------------------------------------
# Win rate
# ---------------------------------------------------------------------------


class TestWinRate:
    def test_three_of_five(self) -> None:
        """3 wins out of 5 trades -> 60% win rate."""
        trades = [
            _trade("10"),
            _trade("20"),
            _trade("-5"),
            _trade("15"),
            _trade("-10"),
        ]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("130")],
            trades=trades,
        )
        expected = Decimal("3") / Decimal("5")
        assert result.win_rate == expected

    def test_all_winners(self) -> None:
        trades = [_trade("10"), _trade("20"), _trade("5")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("135")],
            trades=trades,
        )
        assert result.win_rate == Decimal("1")

    def test_all_losers(self) -> None:
        trades = [_trade("-10"), _trade("-5")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("85")],
            trades=trades,
        )
        assert result.win_rate == Decimal("0")

    def test_no_trades(self) -> None:
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("100")],
            trades=[],
        )
        assert result.win_rate == Decimal("0")
        assert result.total_trades == 0


# ---------------------------------------------------------------------------
# Profit factor
# ---------------------------------------------------------------------------


class TestProfitFactor:
    def test_basic_profit_factor(self) -> None:
        """Sum of wins / sum of losses."""
        trades = [
            _trade("100"),
            _trade("50"),
            _trade("-30"),
            _trade("-20"),
        ]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("200")],
            trades=trades,
        )
        # Gross profit = 150, Gross loss = 50
        assert result.profit_factor == Decimal("3")

    def test_profit_factor_no_losses(self) -> None:
        """No losses -> profit_factor = 0 (edge case, no denominator)."""
        trades = [_trade("100"), _trade("50")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("250")],
            trades=trades,
        )
        assert result.profit_factor == Decimal("0")


# ---------------------------------------------------------------------------
# Deflated Sharpe ratio
# ---------------------------------------------------------------------------


class TestDeflatedSharpe:
    def test_dsr_less_than_raw_sharpe(self) -> None:
        """Deflated Sharpe (as probability) should be less than 1 and typically
        less than the raw Sharpe when there are multiple trials."""
        # Create an equity curve with some variance
        equity = [Decimal("100")]
        for i in range(200):
            change = Decimal("0.5") if i % 3 != 0 else Decimal("-0.3")
            equity.append(equity[-1] + change)

        trades = [_trade("10"), _trade("-5"), _trade("8")]
        result = calculate_metrics(
            equity_curve=equity,
            trades=trades,
            n_trials=10,
        )
        # DSR is a probability (0-1 range) and should be <= 1
        assert result.deflated_sharpe_ratio <= 1.0

    def test_dsr_single_trial(self) -> None:
        """Single trial should still produce a valid DSR."""
        equity = [Decimal("100")]
        for i in range(100):
            change = Decimal("1") if i % 2 == 0 else Decimal("-0.5")
            equity.append(equity[-1] + change)
        result = calculate_metrics(
            equity_curve=equity,
            trades=[],
            n_trials=1,
        )
        assert isinstance(result.deflated_sharpe_ratio, float)


# ---------------------------------------------------------------------------
# Trade dataclass
# ---------------------------------------------------------------------------


class TestTrade:
    def test_duration(self) -> None:
        t = Trade(
            entry_time=T0,
            exit_time=T2,
            symbol="BTCUSDT",
            direction="LONG",
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            quantity=Decimal("1"),
            pnl=Decimal("10"),
            fees=Decimal("0.1"),
        )
        assert t.duration == T2 - T0

    def test_return_pct_long(self) -> None:
        t = Trade(
            entry_time=T0,
            exit_time=T1,
            symbol="BTCUSDT",
            direction="LONG",
            entry_price=Decimal("100"),
            exit_price=Decimal("110"),
            quantity=Decimal("1"),
            pnl=Decimal("10"),
            fees=Decimal("0"),
        )
        assert t.return_pct == Decimal("0.1")

    def test_return_pct_short(self) -> None:
        t = Trade(
            entry_time=T0,
            exit_time=T1,
            symbol="BTCUSDT",
            direction="SHORT",
            entry_price=Decimal("110"),
            exit_price=Decimal("100"),
            quantity=Decimal("1"),
            pnl=Decimal("10"),
            fees=Decimal("0"),
        )
        # Short return = (110 - 100) / 110
        expected = (Decimal("110") - Decimal("100")) / Decimal("110")
        assert t.return_pct == expected


# ---------------------------------------------------------------------------
# BacktestResult dataclass
# ---------------------------------------------------------------------------


class TestBacktestResult:
    def test_default_values(self) -> None:
        result = BacktestResult()
        assert result.total_return == Decimal("0")
        assert result.total_trades == 0
        assert result.sharpe_ratio == 0.0
        assert result.trades == []
        assert result.equity_curve == []
        assert result.benchmark_equity == []
        assert result.benchmark_return == Decimal("0")
        assert result.alpha == 0.0
        assert result.beta == 0.0
        assert result.max_consecutive_wins == 0
        assert result.max_consecutive_losses == 0
        assert result.expectancy == Decimal("0")


# ---------------------------------------------------------------------------
# Benchmark equity (buy-and-hold)
# ---------------------------------------------------------------------------


class TestBenchmarkEquity:
    def test_constant_price(self) -> None:
        """If price stays constant, benchmark equity equals initial capital."""
        closes = [Decimal("100"), Decimal("100"), Decimal("100")]
        result = _compute_benchmark_equity(Decimal("10000"), closes)
        assert result == [Decimal("10000"), Decimal("10000"), Decimal("10000")]

    def test_price_doubles(self) -> None:
        """If price doubles, benchmark equity doubles."""
        closes = [Decimal("100"), Decimal("150"), Decimal("200")]
        result = _compute_benchmark_equity(Decimal("10000"), closes)
        assert result[0] == Decimal("10000")
        assert result[1] == Decimal("15000")
        assert result[2] == Decimal("20000")

    def test_price_halves(self) -> None:
        """If price halves, benchmark equity halves."""
        closes = [Decimal("200"), Decimal("100")]
        result = _compute_benchmark_equity(Decimal("10000"), closes)
        assert result[0] == Decimal("10000")
        assert result[1] == Decimal("5000")

    def test_empty_bars(self) -> None:
        result = _compute_benchmark_equity(Decimal("10000"), [])
        assert result == []

    def test_zero_first_close(self) -> None:
        result = _compute_benchmark_equity(Decimal("10000"), [Decimal("0"), Decimal("100")])
        assert result == []

    def test_via_calculate_metrics(self) -> None:
        """Benchmark equity is populated when bar_closes is provided."""
        equity = [Decimal("10000"), Decimal("10500"), Decimal("11000")]
        closes = [Decimal("100"), Decimal("110"), Decimal("120")]
        result = calculate_metrics(
            equity_curve=equity,
            trades=[],
            bar_closes=closes,
            initial_capital=Decimal("10000"),
        )
        assert len(result.benchmark_equity) == 3
        assert result.benchmark_equity[0] == Decimal("10000")
        assert result.benchmark_equity[1] == Decimal("11000")
        assert result.benchmark_equity[2] == Decimal("12000")
        # benchmark_return = (12000 - 10000) / 10000 = 0.2
        assert result.benchmark_return == Decimal("0.2")

    def test_no_bar_closes(self) -> None:
        """Without bar_closes, benchmark fields stay at defaults."""
        equity = [Decimal("10000"), Decimal("11000")]
        result = calculate_metrics(equity_curve=equity, trades=[])
        assert result.benchmark_equity == []
        assert result.benchmark_return == Decimal("0")
        assert result.alpha == 0.0
        assert result.beta == 0.0


# ---------------------------------------------------------------------------
# Alpha and beta
# ---------------------------------------------------------------------------


class TestAlphaBeta:
    def test_identical_returns(self) -> None:
        """If strategy returns match benchmark exactly, beta=1 and alpha=0."""
        returns = np.array([0.01, -0.005, 0.02, -0.01, 0.015], dtype=np.float64)
        alpha, beta = _compute_alpha_beta(returns, returns, 0.10, 0.10)
        assert beta == pytest.approx(1.0, abs=1e-6)
        assert alpha == pytest.approx(0.0, abs=1e-6)

    def test_uncorrelated_returns(self) -> None:
        """Uncorrelated strategy should have beta near 0."""
        # Strategy and benchmark are independent
        rng = np.random.default_rng(42)
        strategy = rng.normal(0.001, 0.01, 200)
        benchmark = rng.normal(0.001, 0.01, 200)
        _alpha, beta = _compute_alpha_beta(strategy, benchmark, 0.05, 0.05)
        # Beta should be near zero for uncorrelated series
        assert abs(beta) < 0.3

    def test_zero_variance_benchmark(self) -> None:
        """Zero-variance benchmark returns beta=0, alpha=0."""
        strategy = np.array([0.01, 0.02, 0.03], dtype=np.float64)
        benchmark = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        alpha, beta = _compute_alpha_beta(strategy, benchmark, 0.10, 0.0)
        assert beta == 0.0
        assert alpha == 0.0

    def test_short_arrays(self) -> None:
        """Arrays with fewer than 2 elements return (0, 0)."""
        alpha, beta = _compute_alpha_beta(np.array([0.01]), np.array([0.01]), 0.1, 0.1)
        assert alpha == 0.0
        assert beta == 0.0

    def test_strategy_outperforms(self) -> None:
        """Strategy that consistently outperforms should have positive alpha."""
        # Benchmark: varying returns
        benchmark = np.array([0.01, -0.005, 0.015, -0.01, 0.02], dtype=np.float64)
        # Strategy: same pattern but shifted up (higher mean)
        strategy = benchmark + 0.01
        alpha, _beta = _compute_alpha_beta(strategy, benchmark, 0.20, 0.10)
        assert alpha > 0


# ---------------------------------------------------------------------------
# Consecutive wins / losses
# ---------------------------------------------------------------------------


class TestConsecutiveWinsLosses:
    def test_mixed_streak(self) -> None:
        """W, W, W, L, L, W, L -> max_wins=3, max_losses=2."""
        trades = [
            _trade("10"),
            _trade("5"),
            _trade("20"),
            _trade("-10"),
            _trade("-5"),
            _trade("15"),
            _trade("-3"),
        ]
        wins, losses = _compute_consecutive_wins_losses(trades)
        assert wins == 3
        assert losses == 2

    def test_all_wins(self) -> None:
        trades = [_trade("10"), _trade("20"), _trade("5")]
        wins, losses = _compute_consecutive_wins_losses(trades)
        assert wins == 3
        assert losses == 0

    def test_all_losses(self) -> None:
        trades = [_trade("-10"), _trade("-20"), _trade("-5")]
        wins, losses = _compute_consecutive_wins_losses(trades)
        assert wins == 0
        assert losses == 3

    def test_empty_trades(self) -> None:
        wins, losses = _compute_consecutive_wins_losses([])
        assert wins == 0
        assert losses == 0

    def test_single_trade_win(self) -> None:
        wins, losses = _compute_consecutive_wins_losses([_trade("10")])
        assert wins == 1
        assert losses == 0

    def test_breakeven_resets_streak(self) -> None:
        """A breakeven trade (pnl=0) resets both streaks."""
        trades = [_trade("10"), _trade("10"), _trade("0"), _trade("10")]
        wins, _losses = _compute_consecutive_wins_losses(trades)
        assert wins == 2  # First two wins, then breakeven resets, then one more win

    def test_via_calculate_metrics(self) -> None:
        trades = [
            _trade("10"),
            _trade("5"),
            _trade("-10"),
            _trade("-5"),
            _trade("-3"),
            _trade("15"),
        ]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("112")],
            trades=trades,
        )
        assert result.max_consecutive_wins == 2
        assert result.max_consecutive_losses == 3


# ---------------------------------------------------------------------------
# Expectancy
# ---------------------------------------------------------------------------


class TestExpectancy:
    def test_positive_expectancy(self) -> None:
        """Known trades with positive expectancy."""
        trades = [
            _trade("100"),  # win
            _trade("100"),  # win
            _trade("-50"),  # loss
        ]
        exp = _compute_expectancy(trades)
        # win_rate = 2/3, avg_win = 100, loss_rate = 1/3, avg_loss = 50
        # expectancy = (2/3 * 100) - (1/3 * 50) = 66.666... - 16.666... = 50
        expected = (Decimal("2") / Decimal("3")) * Decimal("100") - (
            Decimal("1") / Decimal("3")
        ) * Decimal("50")
        assert exp == expected

    def test_negative_expectancy(self) -> None:
        """All losses produce negative expectancy."""
        trades = [_trade("-10"), _trade("-20")]
        exp = _compute_expectancy(trades)
        # win_rate = 0, loss_rate = 1, avg_loss = 15
        # expectancy = 0 - 1 * 15 = -15
        assert exp == Decimal("-15")

    def test_all_wins(self) -> None:
        """All wins: expectancy = avg_win (loss component = 0)."""
        trades = [_trade("10"), _trade("20")]
        exp = _compute_expectancy(trades)
        # win_rate = 1, avg_win = 15, loss_rate = 0, avg_loss = 0
        assert exp == Decimal("15")

    def test_empty(self) -> None:
        exp = _compute_expectancy([])
        assert exp == Decimal("0")

    def test_via_calculate_metrics(self) -> None:
        trades = [_trade("100"), _trade("-50")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("150")],
            trades=trades,
        )
        # win_rate=0.5, avg_win=100, loss_rate=0.5, avg_loss=50
        # expectancy = 0.5*100 - 0.5*50 = 25
        assert result.expectancy == Decimal("25")
