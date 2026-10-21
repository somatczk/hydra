"""Tests for hydra.backtest.metrics -- BacktestResult, Trade, calculate_metrics."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
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
from hydra.core.types import Timeframe

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
        result = calculate_metrics(equity_curve=equity, trades=[], timeframe=Timeframe.D1)
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
        result = calculate_metrics(equity_curve=equity, trades=[], timeframe=Timeframe.D1)
        assert float(result.max_drawdown) == pytest.approx(0.181818, abs=0.001)


# ---------------------------------------------------------------------------
# Sharpe ratio
# ---------------------------------------------------------------------------


class TestSharpeRatio:
    def test_constant_positive_returns(self) -> None:
        """Zero dispersion -> 0.0, never inf (the API field is a non-null float)."""
        returns = np.array([0.01, 0.01, 0.01, 0.01, 0.01], dtype=np.float64)
        sharpe = _compute_sharpe(returns, 365)
        assert sharpe == 0.0

    def test_zero_returns(self) -> None:
        """Zero returns -> Sharpe = 0."""
        returns = np.array([0.0, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
        sharpe = _compute_sharpe(returns, 365)
        assert sharpe == 0.0

    def test_positive_sharpe(self) -> None:
        """Mix of positive and negative returns with net positive -> positive Sharpe."""
        returns = np.array([0.02, -0.01, 0.03, -0.005, 0.01], dtype=np.float64)
        sharpe = _compute_sharpe(returns, 365)
        assert sharpe > 0

    def test_negative_sharpe(self) -> None:
        """Net negative returns -> negative Sharpe."""
        returns = np.array([-0.02, -0.01, 0.005, -0.03, -0.01], dtype=np.float64)
        sharpe = _compute_sharpe(returns, 365)
        assert sharpe < 0


# ---------------------------------------------------------------------------
# Sortino ratio
# ---------------------------------------------------------------------------


class TestSortinoRatio:
    def test_only_positive_returns(self) -> None:
        """No downside -> undefined (None), not inf."""
        returns = np.array([0.01, 0.02, 0.015, 0.005, 0.03], dtype=np.float64)
        assert _compute_sortino(returns, 365) is None

    def test_only_penalizes_downside(self) -> None:
        """Sortino should be higher than Sharpe when upside volatility is large."""
        # Large upside swings, small downside
        returns = np.array([0.05, -0.001, 0.08, -0.002, 0.1], dtype=np.float64)
        sharpe = _compute_sharpe(returns, 365)
        sortino = _compute_sortino(returns, 365)
        # Sortino should be higher because upside vol doesn't penalize
        assert sortino is not None
        assert sortino > sharpe

    def test_zero_returns(self) -> None:
        returns = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        assert _compute_sortino(returns, 365) is None


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
            timeframe=Timeframe.D1,
        )
        expected = Decimal("3") / Decimal("5")
        assert result.win_rate == expected

    def test_all_winners(self) -> None:
        trades = [_trade("10"), _trade("20"), _trade("5")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("135")],
            trades=trades,
            timeframe=Timeframe.D1,
        )
        assert result.win_rate == Decimal("1")

    def test_all_losers(self) -> None:
        trades = [_trade("-10"), _trade("-5")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("85")],
            trades=trades,
            timeframe=Timeframe.D1,
        )
        assert result.win_rate == Decimal("0")

    def test_no_trades(self) -> None:
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("100")],
            trades=[],
            timeframe=Timeframe.D1,
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
            timeframe=Timeframe.D1,
        )
        # Gross profit = 150, Gross loss = 50
        assert result.profit_factor == Decimal("3")

    def test_profit_factor_no_losses(self) -> None:
        """No losses -> undefined (None), not 0."""
        trades = [_trade("100"), _trade("50")]
        result = calculate_metrics(
            equity_curve=[Decimal("100"), Decimal("250")],
            trades=trades,
            timeframe=Timeframe.D1,
        )
        assert result.profit_factor is None


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
        """Benchmark equity is populated when benchmark_prices is provided."""
        equity = [Decimal("10000"), Decimal("10500"), Decimal("11000")]
        closes = [Decimal("100"), Decimal("110"), Decimal("120")]
        result = calculate_metrics(
            equity_curve=equity,
            trades=[],
            timeframe=Timeframe.D1,
            benchmark_prices=closes,
            initial_capital=Decimal("10000"),
        )
        assert len(result.benchmark_equity) == 3
        assert result.benchmark_equity[0] == Decimal("10000")
        assert result.benchmark_equity[1] == Decimal("11000")
        assert result.benchmark_equity[2] == Decimal("12000")
        # benchmark_return = (12000 - 10000) / 10000 = 0.2
        assert result.benchmark_return == Decimal("0.2")

    def test_no_benchmark_prices(self) -> None:
        """Without benchmark_prices, benchmark fields stay at defaults."""
        equity = [Decimal("10000"), Decimal("11000")]
        result = calculate_metrics(equity_curve=equity, trades=[], timeframe=Timeframe.D1)
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
        alpha, beta = _compute_alpha_beta(returns, returns, 365)
        assert beta == pytest.approx(1.0, abs=1e-6)
        assert alpha == pytest.approx(0.0, abs=1e-6)

    def test_uncorrelated_returns(self) -> None:
        """Uncorrelated strategy should have beta near 0."""
        # Strategy and benchmark are independent
        rng = np.random.default_rng(42)
        strategy = rng.normal(0.001, 0.01, 200)
        benchmark = rng.normal(0.001, 0.01, 200)
        _alpha, beta = _compute_alpha_beta(strategy, benchmark, 365)
        # Beta should be near zero for uncorrelated series
        assert abs(beta) < 0.3

    def test_zero_variance_benchmark(self) -> None:
        """Zero-variance benchmark returns beta=0, alpha=0."""
        strategy = np.array([0.01, 0.02, 0.03], dtype=np.float64)
        benchmark = np.array([0.0, 0.0, 0.0], dtype=np.float64)
        alpha, beta = _compute_alpha_beta(strategy, benchmark, 365)
        assert beta == 0.0
        assert alpha == 0.0

    def test_short_arrays(self) -> None:
        """Arrays with fewer than 2 elements return (0, 0)."""
        alpha, beta = _compute_alpha_beta(np.array([0.01]), np.array([0.01]), 365)
        assert alpha == 0.0
        assert beta == 0.0

    def test_strategy_outperforms(self) -> None:
        """Strategy that consistently outperforms should have positive alpha."""
        # Benchmark: varying returns
        benchmark = np.array([0.01, -0.005, 0.015, -0.01, 0.02], dtype=np.float64)
        # Strategy: same pattern but shifted up (higher mean)
        strategy = benchmark + 0.01
        alpha, _beta = _compute_alpha_beta(strategy, benchmark, 365)
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
            timeframe=Timeframe.D1,
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
            timeframe=Timeframe.D1,
        )
        # win_rate=0.5, avg_win=100, loss_rate=0.5, avg_loss=50
        # expectancy = 0.5*100 - 0.5*50 = 25
        assert result.expectancy == Decimal("25")


# ---------------------------------------------------------------------------
# Exact reference values (hand-derived; 24/7 annualisation by bar size)
# ---------------------------------------------------------------------------

# Returns [0.1, -0.1, 0, 0.1]: mean 0.025, std(ddof=1) 0.0957427107756338, max dd 0.1 over 3 bars.
_REF = [Decimal("100"), Decimal("110"), Decimal("99"), Decimal("99"), Decimal("108.9")]
_DAILY_SHARPE = 4.988623420981349  # 0.025 / 0.0957427107756338 * sqrt(365)


def _ref(timeframe: Timeframe) -> BacktestResult:
    return calculate_metrics(_REF, [], timeframe=timeframe)


class TestReferenceValues:
    def test_sharpe_daily_reference(self) -> None:
        assert _ref(Timeframe.D1).sharpe_ratio == pytest.approx(_DAILY_SHARPE, rel=1e-12)

    def test_sharpe_scales_with_bar_size(self) -> None:
        """Same returns on 1h bars: ppy 8760, so daily * sqrt(24); 1m: daily * sqrt(1440)."""
        assert _ref(Timeframe.H1).sharpe_ratio == pytest.approx(24.439163800603485, rel=1e-12)
        assert _ref(Timeframe.M1).sharpe_ratio == pytest.approx(
            _DAILY_SHARPE * math.sqrt(1440), rel=1e-12
        )

    def test_sortino_uses_all_n(self) -> None:
        """tdd = sqrt((0 + 0.01 + 0 + 0) / 4) = 0.05 -> 0.025 / 0.05 * sqrt(365)."""
        assert _ref(Timeframe.D1).sortino_ratio == pytest.approx(9.552486587271403, rel=1e-12)

    def test_sortino_none_without_downside(self) -> None:
        equity = [Decimal("100"), Decimal("101"), Decimal("101"), Decimal("103")]
        assert calculate_metrics(equity, [], timeframe=Timeframe.D1).sortino_ratio is None

    def test_cagr_hourly_year(self) -> None:
        """8760 hourly returns compounding to +63.6 % is one year: CAGR 0.636, not 0.0143.

        Constant growth has zero dispersion up to float noise, so Sharpe is 0.0, not 9e12.
        """
        g = 1.636 ** (1 / 8760)
        equity = [Decimal(str(100 * g**i)) for i in range(8761)]
        result = calculate_metrics(equity, [], timeframe=Timeframe.H1)
        assert float(result.annualized_return) == pytest.approx(0.636, abs=1e-6)
        assert result.sharpe_ratio == 0.0

    def test_calmar(self) -> None:
        """CAGR = 1.089 ** (365 / 4) - 1 = 2391.180950741495; Calmar = CAGR / 0.1."""
        cagr = 1.089 ** (365 / 4) - 1
        assert _ref(Timeframe.D1).calmar_ratio == pytest.approx(cagr / 0.1, rel=1e-9)
        rising = [Decimal("100"), Decimal("101"), Decimal("102")]
        assert calculate_metrics(rising, [], timeframe=Timeframe.D1).calmar_ratio is None

    def test_max_drawdown_and_duration_in_time(self) -> None:
        """Bars 2, 3 and 4 sit below the 110 peak: 3 bars of 1h, not 3 days."""
        result = _ref(Timeframe.H1)
        assert result.max_drawdown == Decimal("0.1")
        assert result.max_drawdown_duration == timedelta(hours=3)

    def test_alpha_beta_exact(self) -> None:
        """Strategy returns s = 0.5 * b + 0.001 -> beta 0.5, Jensen alpha 0.001 * 365."""
        b = [0.02, -0.01, 0.03, -0.02]
        prices, equity = [100.0], [1000.0]
        for r in b:
            prices.append(prices[-1] * (1 + r))
            equity.append(equity[-1] * (1 + 0.5 * r + 0.001))
        result = calculate_metrics(
            [Decimal(str(e)) for e in equity],
            [],
            timeframe=Timeframe.D1,
            benchmark_prices=[Decimal(str(p)) for p in prices],
            initial_capital=Decimal("1000"),
        )
        assert result.beta == pytest.approx(0.5, abs=1e-12)
        assert result.alpha == pytest.approx(0.365, abs=1e-9)

    def test_buy_and_hold_vs_own_benchmark(self) -> None:
        """Runner-shaped input (pre-seeded initial point at the first close) is beta 1, alpha 0.

        At main the benchmark was one point shorter and shifted, giving beta -0.369.
        """
        closes = [Decimal(c) for c in ("100", "103", "99", "104", "101", "107")]
        prices = [closes[0], *closes]
        initial = Decimal("10000")
        equity = [initial * p / closes[0] for p in prices]
        result = calculate_metrics(
            equity,
            [],
            timeframe=Timeframe.H1,
            benchmark_prices=prices,
            initial_capital=initial,
        )
        assert result.benchmark_equity == equity
        assert result.beta == pytest.approx(1.0, abs=1e-12)
        assert result.alpha == pytest.approx(0.0, abs=1e-12)

    def test_benchmark_length_mismatch_raises(self) -> None:
        with pytest.raises(ValueError, match="benchmark_prices"):
            calculate_metrics(_REF, [], timeframe=Timeframe.D1, benchmark_prices=[Decimal("1")] * 4)
        with pytest.raises(ValueError, match="timestamps"):
            calculate_metrics(_REF, [], timeframe=Timeframe.D1, timestamps=[T0, T1])

    def test_trade_stats_exact(self) -> None:
        trades = [_trade(p) for p in ("30", "-10", "-10", "0", "50")]
        result = calculate_metrics(_REF, trades, timeframe=Timeframe.D1)
        assert result.win_rate == Decimal("0.4")
        assert result.profit_factor == Decimal("4")
        assert result.expectancy == Decimal("12")
        assert (result.avg_win, result.avg_loss) == (Decimal("40"), Decimal("10"))

    def test_no_metric_is_inf_or_nan(self) -> None:
        flat = [Decimal("100")] * 5
        for equity in (flat, _REF, [Decimal("100")]):
            result = calculate_metrics(equity, [], timeframe=Timeframe.H1)
            for value in (result.sharpe_ratio, result.sortino_ratio, result.calmar_ratio):
                assert value is None or math.isfinite(value)
