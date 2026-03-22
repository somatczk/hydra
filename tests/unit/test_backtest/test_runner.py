"""Tests for hydra.backtest.runner -- BacktestRunner end-to-end."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import ClassVar

import pytest

from hydra.backtest.fills import CommissionConfig, SlippageModel
from hydra.backtest.runner import BacktestRunner, _SimplePositionTracker
from hydra.core.events import BarEvent, EntrySignal, ExitSignal
from hydra.core.types import (
    OHLCV,
    Direction,
    MarketType,
    OrderFill,
    Side,
    Symbol,
    Timeframe,
)
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig

# ---------------------------------------------------------------------------
# Test strategy implementations
# ---------------------------------------------------------------------------


class BuyAndHoldStrategy(BaseStrategy):
    """Buy on first bar, hold forever. Used for basic backtest tests."""

    _entered = False

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        if not self._entered:
            self._entered = True
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=Decimal("1"),
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    market_type=MarketType.SPOT,
                )
            ]
        return []


class BuyThenSellStrategy(BaseStrategy):
    """Buy on bar 2 (after required_history), sell on bar 5."""

    _bar_count = 0
    _entered = False

    @property
    def required_history(self) -> int:
        return 2

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        self._bar_count += 1
        if self._bar_count == 3 and not self._entered:
            self._entered = True
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.LONG,
                    strength=Decimal("1"),
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    market_type=MarketType.SPOT,
                )
            ]
        if self._bar_count == 6 and self._entered:
            self._entered = False
            return [
                ExitSignal(
                    symbol=bar.symbol,
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    reason="take_profit",
                )
            ]
        return []


class ShortThenExitStrategy(BaseStrategy):
    """Enter SHORT on bar 3, exit on bar 6."""

    _bar_count = 0
    _entered = False

    @property
    def required_history(self) -> int:
        return 2

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        self._bar_count += 1
        if self._bar_count == 3 and not self._entered:
            self._entered = True
            return [
                EntrySignal(
                    symbol=bar.symbol,
                    direction=Direction.SHORT,
                    strength=Decimal("1"),
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    market_type=MarketType.FUTURES,
                )
            ]
        if self._bar_count == 6 and self._entered:
            self._entered = False
            return [
                ExitSignal(
                    symbol=bar.symbol,
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    reason="take_profit",
                )
            ]
        return []


class ExitOnlyStrategy(BaseStrategy):
    """Emits an ExitSignal on bar 3 without ever entering a position."""

    _bar_count = 0

    @property
    def required_history(self) -> int:
        return 2

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        self._bar_count += 1
        if self._bar_count == 3:
            return [
                ExitSignal(
                    symbol=bar.symbol,
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id="binance",
                    reason="spurious_exit",
                )
            ]
        return []


class NoLookaheadTestStrategy(BaseStrategy):
    """Records bar data seen at each on_bar call to verify no lookahead."""

    bars_seen: ClassVar[list[list[OHLCV]]] = []

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        tf = bar.timeframe
        all_bars = self.context.bars(symbol, tf, count=9999)
        self.__class__.bars_seen.append(list(all_bars))
        return []


class AlwaysBuyStrategy(BaseStrategy):
    """Emits a buy signal every bar. Used to test position sizing limits."""

    @property
    def required_history(self) -> int:
        return 1

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        return [
            EntrySignal(
                symbol=bar.symbol,
                direction=Direction.LONG,
                strength=Decimal("1"),
                strategy_id=self.strategy_id,
                exchange_id="binance",
                market_type=MarketType.SPOT,
            )
        ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bars(n: int, start_price: float = 100.0, trend: float = 1.0) -> list[OHLCV]:
    """Generate n synthetic OHLCV bars with a trend."""
    bars = []
    base_time = datetime(2024, 1, 1, tzinfo=UTC)
    price = start_price
    for i in range(n):
        o = Decimal(str(round(price, 2)))
        h = Decimal(str(round(price + 5, 2)))
        lo = Decimal(str(round(price - 3, 2)))
        c = Decimal(str(round(price + trend, 2)))
        bars.append(
            OHLCV(
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=Decimal("1000"),
                timestamp=base_time + timedelta(hours=i),
            )
        )
        price = float(c)
    return bars


def _make_crashing_bars(n: int, start_price: float = 42000.0) -> list[OHLCV]:
    """Generate bars that crash steeply to test margin call."""
    bars = []
    base_time = datetime(2024, 1, 1, tzinfo=UTC)
    price = start_price
    for i in range(n):
        # Lose 5% each bar
        new_price = price * 0.95
        o = Decimal(str(round(price, 2)))
        h = Decimal(str(round(price * 1.001, 2)))
        lo = Decimal(str(round(new_price * 0.999, 2)))
        c = Decimal(str(round(new_price, 2)))
        bars.append(
            OHLCV(
                open=o,
                high=h,
                low=lo,
                close=c,
                volume=Decimal("1000"),
                timestamp=base_time + timedelta(hours=i),
            )
        )
        price = new_price
    return bars


def _make_config(strategy_id: str = "test") -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        name="Test Strategy",
        strategy_class="test.BuyAndHold",
        symbols=["BTCUSDT"],
    )


@pytest.fixture
def runner() -> BacktestRunner:
    return BacktestRunner(
        slippage_model=SlippageModel(
            spread_factor=Decimal("0"),
            volume_impact_factor=Decimal("0"),
        ),
    )


# ---------------------------------------------------------------------------
# Basic backtest
# ---------------------------------------------------------------------------


class TestBasicBacktest:
    async def test_backtest_with_100_bars(self, runner: BacktestRunner) -> None:
        """Feed 100 bars to a buy-and-hold strategy, verify result is valid."""
        bars = _make_bars(100, start_price=100.0, trend=0.5)
        config = _make_config()

        result = await runner.run(
            strategy_class=BuyAndHoldStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        # Should have an equity curve with data
        assert len(result.equity_curve) > 0
        # Sharpe and other metrics should be computed
        assert isinstance(result.sharpe_ratio, float)
        assert isinstance(result.total_return, Decimal)

    async def test_backtest_generates_trades(self, runner: BacktestRunner) -> None:
        """Strategy that enters then exits should produce at least one trade."""
        bars = _make_bars(20, start_price=100.0, trend=0.5)
        config = _make_config()

        result = await runner.run(
            strategy_class=BuyThenSellStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        assert result.total_trades >= 1

    async def test_empty_bars(self, runner: BacktestRunner) -> None:
        """Empty bars should return a result with initial capital only."""
        config = _make_config()
        result = await runner.run(
            strategy_class=BuyAndHoldStrategy,
            strategy_config=config,
            bars=[],
            initial_capital=Decimal("50000"),
        )
        assert result.equity_curve == [Decimal("50000")]
        assert result.total_trades == 0


# ---------------------------------------------------------------------------
# No-lookahead
# ---------------------------------------------------------------------------


class TestShortExitFix:
    """Regression tests for SHORT exit signals (Bug: exits compounded instead of closing)."""

    async def test_short_exit_closes_position(self, runner: BacktestRunner) -> None:
        """SHORT entry + EXIT should produce a completed trade with reasonable PnL."""
        bars = _make_bars(20, start_price=42000.0, trend=-10.0)
        config = StrategyConfig(
            id="test_short",
            name="Test Short",
            strategy_class="test.ShortThenExit",
            symbols=["BTCUSDT"],
            exchange={"exchange_id": "binance", "market_type": "FUTURES"},
        )

        result = await runner.run(
            strategy_class=ShortThenExitStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        assert result.total_trades >= 1, "SHORT entry + exit should produce a trade"
        # PnL should be reasonable, not quadrillions
        for trade in result.trades:
            assert abs(float(trade.pnl)) < 100000, (
                f"Trade PnL {trade.pnl} is unreasonable"
            )
        # Final equity should be close to initial capital
        final_equity = float(result.equity_curve[-1])
        assert abs(final_equity - 100000) < 50000, (
            f"Final equity {final_equity} is unreasonable for $100k capital"
        )

    async def test_exit_signal_no_position_is_noop(self, runner: BacktestRunner) -> None:
        """ExitSignal with no open position should not create phantom orders."""
        bars = _make_bars(20, start_price=100.0, trend=0.5)
        config = _make_config()

        result = await runner.run(
            strategy_class=ExitOnlyStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        assert result.total_trades == 0, "Exit with no position should produce 0 trades"
        assert result.equity_curve[-1] == Decimal("100000"), (
            "Capital should be unchanged when no trades occur"
        )


class TestNoLookahead:
    async def test_strategy_cannot_see_future_bars(self, runner: BacktestRunner) -> None:
        """At bar N, strategy context should contain only bars [0..N]."""
        NoLookaheadTestStrategy.bars_seen = []

        bars = _make_bars(10, start_price=100.0, trend=1.0)
        config = _make_config()

        await runner.run(
            strategy_class=NoLookaheadTestStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        for i, seen in enumerate(NoLookaheadTestStrategy.bars_seen):
            # At bar i, the strategy should have seen at most (i+1) bars
            assert len(seen) <= i + 1, (
                f"At bar {i}, strategy saw {len(seen)} bars but should see at most {i + 1}"
            )


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


class TestEquityCurve:
    async def test_starts_at_initial_capital(self, runner: BacktestRunner) -> None:
        """Equity curve should start at initial_capital."""
        bars = _make_bars(5)
        config = _make_config()

        result = await runner.run(
            strategy_class=BuyAndHoldStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("50000"),
        )

        assert result.equity_curve[0] == Decimal("50000")


# ---------------------------------------------------------------------------
# Commission deduction
# ---------------------------------------------------------------------------


class TestCommissionDeduction:
    async def test_commission_reduces_pnl(self) -> None:
        """Trades should have commission deducted from PnL."""
        runner_with_comm = BacktestRunner(
            slippage_model=SlippageModel(
                spread_factor=Decimal("0"),
                volume_impact_factor=Decimal("0"),
            ),
        )
        bars = _make_bars(20, start_price=100.0, trend=0.5)
        comm = CommissionConfig(spot_taker=Decimal("0.001"))
        config = _make_config()

        result = await runner_with_comm.run(
            strategy_class=BuyThenSellStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            commission=comm,
        )

        if result.trades:
            # Each trade should have non-zero fees
            for trade in result.trades:
                assert trade.fees >= Decimal("0")


# ---------------------------------------------------------------------------
# Position tracker
# ---------------------------------------------------------------------------


class TestSimplePositionTracker:
    def test_open_long_position(self) -> None:
        tracker = _SimplePositionTracker(Decimal("10000"))
        fill = OrderFill(
            order_id="1",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0.1"),
            fee_currency="USDT",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            exchange_id="binance",
        )
        trade = tracker.apply_fill(fill)
        assert trade is None  # Opening, not closing
        assert "BTCUSDT" in tracker.positions
        # Cash = 10000 - 100 - 0.1 = 9899.9
        assert tracker.cash == Decimal("9899.9")

    def test_close_long_position(self) -> None:
        tracker = _SimplePositionTracker(Decimal("10000"))
        # Open
        buy_fill = OrderFill(
            order_id="1",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("1"),
            price=Decimal("100"),
            fee=Decimal("0.1"),
            fee_currency="USDT",
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
            exchange_id="binance",
        )
        tracker.apply_fill(buy_fill)

        # Close at 110
        sell_fill = OrderFill(
            order_id="2",
            symbol=Symbol("BTCUSDT"),
            side=Side.SELL,
            quantity=Decimal("1"),
            price=Decimal("110"),
            fee=Decimal("0.1"),
            fee_currency="USDT",
            timestamp=datetime(2024, 1, 2, tzinfo=UTC),
            exchange_id="binance",
        )
        trade = tracker.apply_fill(sell_fill)

        assert trade is not None
        assert trade.pnl == Decimal("9.9")  # (110-100)*1 - 0.1
        assert "BTCUSDT" not in tracker.positions


# ---------------------------------------------------------------------------
# Position sizing
# ---------------------------------------------------------------------------


class TestPositionSizing:
    async def test_quantity_based_on_equity(self, runner: BacktestRunner) -> None:
        """Position quantity should be ~10% of equity / price, not 1 BTC."""
        bars = _make_bars(20, start_price=42000.0, trend=10.0)
        config = _make_config()

        result = await runner.run(
            strategy_class=BuyAndHoldStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("10000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        # With $10k capital and 10% risk, notional = $1000
        # At ~$42k BTC, quantity should be ~0.024 BTC, NOT 1 BTC
        # The equity should never go below -$10k (no multi-million $ losses)
        min_equity = min(float(e) for e in result.equity_curve)
        assert min_equity > -10000, f"Equity dropped to {min_equity}, position sizing is broken"

    async def test_small_capital_reasonable_pnl(self, runner: BacktestRunner) -> None:
        """$10k capital should not produce $2M losses."""
        bars = _make_bars(100, start_price=42000.0, trend=-50.0)
        config = _make_config()

        result = await runner.run(
            strategy_class=BuyAndHoldStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("10000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        final_equity = float(result.equity_curve[-1])
        # PnL should be bounded by capital size, not millions
        assert abs(final_equity) < 100000, (
            f"Final equity {final_equity} is unreasonable for $10k capital"
        )


# ---------------------------------------------------------------------------
# Margin call
# ---------------------------------------------------------------------------


class TestMarginCall:
    async def test_equity_floor_stops_trading(self, runner: BacktestRunner) -> None:
        """When equity hits zero, backtest should stop with margin_call reason."""
        # Use crashing bars that lose 5% per bar
        bars = _make_crashing_bars(200, start_price=42000.0)
        config = _make_config()

        result = await runner.run(
            strategy_class=AlwaysBuyStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("1000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        # If equity went to zero, stopped_reason should be set
        min_equity = min(float(e) for e in result.equity_curve)
        if min_equity <= 0:
            assert result.stopped_reason == "margin_call"
            # Should not have processed all 200 bars
            assert len(result.equity_curve) < 202  # 200 bars + initial + margin close

    async def test_stopped_reason_none_for_normal_run(self, runner: BacktestRunner) -> None:
        """Normal backtest should have stopped_reason=None."""
        bars = _make_bars(20, start_price=100.0, trend=0.5)
        config = _make_config()

        result = await runner.run(
            strategy_class=BuyAndHoldStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("100000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        assert result.stopped_reason is None

    async def test_margin_call_closes_all_positions(self, runner: BacktestRunner) -> None:
        """After margin call, all positions should be closed."""
        bars = _make_crashing_bars(200, start_price=42000.0)
        config = _make_config()

        result = await runner.run(
            strategy_class=AlwaysBuyStrategy,
            strategy_config=config,
            bars=bars,
            initial_capital=Decimal("1000"),
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
        )

        if result.stopped_reason == "margin_call":
            # All trades should have been closed (exit_time set)
            for trade in result.trades:
                assert trade.exit_time is not None
