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
