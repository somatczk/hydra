"""Tests for PaperTradingExecutor: market fills, limit pending, balance, positions."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.types import OHLCV
from hydra.execution.paper_trading import PaperTradingExecutor

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_bar(
    open_: str = "42000",
    high: str = "42500",
    low: str = "41500",
    close: str = "42100",
    volume: str = "100",
) -> OHLCV:
    return OHLCV(
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        timestamp=datetime(2024, 6, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Market order tests
# ---------------------------------------------------------------------------


class TestMarketOrders:
    async def test_market_buy_immediate_fill(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0.001"),
            fee_pct=Decimal("0.001"),
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))

        result = await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="MARKET",
            quantity=Decimal("0.1"),
        )
        assert result["status"] == "FILLED"
        assert result["filled"] is True
        assert result["id"]

    async def test_market_sell_immediate_fill(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))

        # Buy first to have a position
        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )
        result = await executor.create_order(
            symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=Decimal("0.1")
        )
        assert result["status"] == "FILLED"

    async def test_market_order_no_price_raises(self) -> None:
        executor = PaperTradingExecutor()
        with pytest.raises(ValueError, match="No market price"):
            await executor.create_order(
                symbol="UNKNOWN",
                side="BUY",
                order_type="MARKET",
                quantity=Decimal("1"),
            )

    async def test_market_buy_slippage_applied(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0.01"),  # 1% slippage for easy calculation
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("10000"))

        result = await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        # Buy slippage: 10000 * 1.01 = 10100
        assert Decimal(str(result["price"])) == Decimal("10100")


# ---------------------------------------------------------------------------
# Limit / Stop order tests
# ---------------------------------------------------------------------------


class TestPendingOrders:
    async def test_limit_order_goes_pending(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        result = await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.1"),
            price=Decimal("40000"),
        )
        assert result["status"] == "PENDING"
        assert result["filled"] is False

    async def test_limit_buy_fills_when_low_reaches_price(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.1"),
            price=Decimal("41000"),
        )

        # Bar where low touches the limit price
        bar = _make_bar(low="40900")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 1
        assert fills[0]["status"] == "FILLED"

    async def test_limit_buy_not_filled_when_low_above_price(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.1"),
            price=Decimal("40000"),
        )
        bar = _make_bar(low="41000")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 0

    async def test_limit_sell_fills_when_high_reaches_price(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        # Buy first
        executor.set_market_price("BTCUSDT", Decimal("42000"))
        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )
        # Place limit sell
        await executor.create_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="LIMIT",
            quantity=Decimal("0.1"),
            price=Decimal("43000"),
        )
        bar = _make_bar(high="43500")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 1

    async def test_stop_market_buy_triggers(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="STOP_MARKET",
            quantity=Decimal("0.1"),
            stop_price=Decimal("43000"),
        )
        bar = _make_bar(high="43500")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 1

    async def test_cancel_pending_order(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        result = await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("0.1"),
            price=Decimal("40000"),
        )
        oid = result["id"]
        cancel_result = await executor.cancel_order(oid, "BTCUSDT")
        assert cancel_result["status"] == "CANCELLED"

        # Pending list should be empty
        open_orders = await executor.fetch_open_orders()
        assert len(open_orders) == 0


# ---------------------------------------------------------------------------
# Balance tracking
# ---------------------------------------------------------------------------


class TestBalanceTracking:
    async def test_balance_decreases_on_buy(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        balances = await executor.fetch_balance()
        assert balances["USDT"] == Decimal("50000")

    async def test_balance_increases_on_sell(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=Decimal("1")
        )
        balances = await executor.fetch_balance()
        assert balances["USDT"] == Decimal("100000")

    async def test_insufficient_balance_raises(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        with pytest.raises(ValueError, match="Insufficient"):
            await executor.create_order(
                symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
            )


# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------


class TestPositionTracking:
    async def test_position_created_on_buy(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )
        positions = await executor.fetch_positions()
        assert len(positions) == 1
        assert positions[0]["symbol"] == "BTCUSDT"
        assert positions[0]["side"] == "long"

    async def test_position_closed_on_opposing_trade(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )
        await executor.create_order(
            symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=Decimal("0.1")
        )
        positions = await executor.fetch_positions()
        assert len(positions) == 0  # Position is flat, excluded

    async def test_position_filter_by_symbol(self) -> None:
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("200000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("42000"))
        executor.set_market_price("ETHUSDT", Decimal("3000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("0.1")
        )
        await executor.create_order(
            symbol="ETHUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )

        btc_positions = await executor.fetch_positions(symbol="BTCUSDT")
        assert len(btc_positions) == 1
        assert btc_positions[0]["symbol"] == "BTCUSDT"


# ---------------------------------------------------------------------------
# Trailing stop order tests
# ---------------------------------------------------------------------------


class TestTrailingStopOrders:
    """Tests for TRAILING_STOP order type."""

    async def test_trailing_stop_sell_updates_peak_as_price_rises(self) -> None:
        """Peak price tracks upward with bar highs for SELL trailing stops."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        # Open a long position, then place a SELL trailing stop to protect it
        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="TRAILING_STOP",
            quantity=Decimal("1"),
            params={"trail_pct": "0.05"},  # 5% trail
        )

        # Price rises to 55000 -- peak updates; low stays above trigger (55000*0.95=52250)
        bar1 = _make_bar(open_="50000", high="55000", low="53000", close="54000")
        fills = executor.check_pending_orders(bar1)
        assert len(fills) == 0

        # Price rises further to 60000 -- peak updates; low above trigger (60000*0.95=57000)
        bar2 = _make_bar(open_="54000", high="60000", low="58000", close="59000")
        fills = executor.check_pending_orders(bar2)
        assert len(fills) == 0

        # Peak is now 60000. Trigger = 60000 * 0.95 = 57000.
        # A bar with low=57500 should NOT trigger
        bar3 = _make_bar(open_="59000", high="59500", low="57500", close="58000")
        fills = executor.check_pending_orders(bar3)
        assert len(fills) == 0

        # A bar with low=56500 SHOULD trigger (below 57000)
        bar4 = _make_bar(open_="58000", high="58500", low="56500", close="57000")
        fills = executor.check_pending_orders(bar4)
        assert len(fills) == 1
        assert fills[0]["status"] == "FILLED"
        assert fills[0]["side"] == "SELL"

    async def test_trailing_stop_sell_triggers_on_reversal(self) -> None:
        """SELL trailing stop triggers when price drops past trail_pct from peak."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("10000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="TRAILING_STOP",
            quantity=Decimal("1"),
            params={"trail_pct": "0.10"},  # 10% trail
        )

        # Peak is initialized at 10000. Trigger = 10000 * 0.90 = 9000.
        # Bar drops to 8900 -- should trigger immediately
        bar = _make_bar(open_="10000", high="10000", low="8900", close="9100")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 1
        assert fills[0]["status"] == "FILLED"

    async def test_trailing_stop_sell_does_not_trigger_while_trending_up(self) -> None:
        """SELL trailing stop stays pending while price only moves up."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("10000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="TRAILING_STOP",
            quantity=Decimal("1"),
            params={"trail_pct": "0.05"},  # 5% trail
        )

        # Successive bars trending upward -- trail never triggers
        for price in ["11000", "12000", "13000", "14000", "15000"]:
            low = str(Decimal(price) - Decimal("200"))
            bar = _make_bar(open_=price, high=price, low=low, close=price)
            fills = executor.check_pending_orders(bar)
            assert len(fills) == 0

        # Confirm order is still pending
        open_orders = await executor.fetch_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0]["type"] == "TRAILING_STOP"

    async def test_trailing_stop_buy_updates_trough_as_price_falls(self) -> None:
        """Peak (trough) price tracks downward with bar lows for BUY trailing stops."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        # Open a short position, then place a BUY trailing stop to protect it
        await executor.create_order(
            symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="TRAILING_STOP",
            quantity=Decimal("1"),
            params={"trail_pct": "0.05"},  # 5% trail
        )

        # Price drops to 45000 -- trough updates; high stays below trigger (45000*1.05=47250)
        bar1 = _make_bar(open_="49000", high="47000", low="45000", close="46000")
        fills = executor.check_pending_orders(bar1)
        assert len(fills) == 0

        # Price drops further to 40000 -- trough updates; high below trigger (40000*1.05=42000)
        bar2 = _make_bar(open_="46000", high="41500", low="40000", close="41000")
        fills = executor.check_pending_orders(bar2)
        assert len(fills) == 0

        # Trough updates to 39500 (min(40000, 39500)). Trigger = 39500 * 1.05 = 41475.
        # Bar with high=41000 should NOT trigger
        bar3 = _make_bar(open_="41000", high="41000", low="39500", close="40500")
        fills = executor.check_pending_orders(bar3)
        assert len(fills) == 0

        # Trough is now 39500. Trigger = 39500 * 1.05 = 41475.
        # Bar with high=42000 SHOULD trigger
        bar4 = _make_bar(open_="40500", high="42000", low="40000", close="41800")
        fills = executor.check_pending_orders(bar4)
        assert len(fills) == 1
        assert fills[0]["status"] == "FILLED"
        assert fills[0]["side"] == "BUY"

    async def test_trailing_stop_buy_triggers_on_reversal(self) -> None:
        """BUY trailing stop triggers when price rises past trail_pct from trough."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0"),
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("10000"))

        await executor.create_order(
            symbol="BTCUSDT", side="SELL", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="BUY",
            order_type="TRAILING_STOP",
            quantity=Decimal("1"),
            params={"trail_pct": "0.10"},  # 10% trail
        )

        # Peak initialized at 10000. Trigger = 10000 * 1.10 = 11000.
        # Bar high 11500 -- should trigger immediately
        bar = _make_bar(open_="10000", high="11500", low="9800", close="11000")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 1
        assert fills[0]["status"] == "FILLED"

    async def test_trailing_stop_fill_price_includes_slippage(self) -> None:
        """Trailing stop fill price applies slippage to the trigger price."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
            slippage_pct=Decimal("0.01"),  # 1% slippage for easy calculation
            fee_pct=Decimal("0"),
        )
        executor.set_market_price("BTCUSDT", Decimal("10000"))

        await executor.create_order(
            symbol="BTCUSDT", side="BUY", order_type="MARKET", quantity=Decimal("1")
        )
        await executor.create_order(
            symbol="BTCUSDT",
            side="SELL",
            order_type="TRAILING_STOP",
            quantity=Decimal("1"),
            params={"trail_pct": "0.10"},  # 10% trail
        )

        # Peak = 10000, trigger = 10000 * 0.90 = 9000
        # Sell slippage: 9000 * (1 - 0.01) = 8910
        bar = _make_bar(open_="10000", high="10000", low="8800", close="9000")
        fills = executor.check_pending_orders(bar)
        assert len(fills) == 1
        assert Decimal(str(fills[0]["price"])) == Decimal("8910")

    async def test_trailing_stop_requires_trail_pct(self) -> None:
        """Creating a TRAILING_STOP without trail_pct raises ValueError."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        executor.set_market_price("BTCUSDT", Decimal("50000"))

        with pytest.raises(ValueError, match="trail_pct is required"):
            await executor.create_order(
                symbol="BTCUSDT",
                side="SELL",
                order_type="TRAILING_STOP",
                quantity=Decimal("1"),
            )

    async def test_trailing_stop_requires_market_price(self) -> None:
        """Creating a TRAILING_STOP without a known market price raises ValueError."""
        executor = PaperTradingExecutor(
            initial_balances={"USDT": Decimal("100000")},
        )
        # No market price set for BTCUSDT
        with pytest.raises(ValueError, match="No market price"):
            await executor.create_order(
                symbol="BTCUSDT",
                side="SELL",
                order_type="TRAILING_STOP",
                quantity=Decimal("1"),
                params={"trail_pct": "0.05"},
            )
