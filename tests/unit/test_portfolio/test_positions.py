"""Tests for PositionTracker: open, add, reduce, close, price updates, filters."""

from __future__ import annotations

from decimal import Decimal

from hydra.core.events import OrderFillEvent
from hydra.core.types import Direction, Side, Symbol
from hydra.portfolio.positions import PositionTracker

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SYMBOL = Symbol("BTCUSDT")
EXCHANGE = "binance"
STRATEGY = "momentum_v1"


def _fill(
    *,
    side: Side = Side.BUY,
    quantity: str = "1",
    price: str = "40000",
    fee: str = "10",
    symbol: str = SYMBOL,
    exchange_id: str = EXCHANGE,
) -> OrderFillEvent:
    return OrderFillEvent(
        order_id="ord-1",
        symbol=Symbol(symbol),
        side=side,
        quantity=Decimal(quantity),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_currency="USDT",
        exchange_id=exchange_id,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOpenNewPosition:
    async def test_buy_opens_long(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(_fill(side=Side.BUY), strategy_id=STRATEGY)

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        assert pos.direction == Direction.LONG
        assert pos.quantity == Decimal("1")
        assert pos.avg_entry_price == Decimal("40000")
        assert pos.strategy_id == STRATEGY

    async def test_sell_opens_short(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(_fill(side=Side.SELL), strategy_id=STRATEGY)

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        assert pos.direction == Direction.SHORT
        assert pos.quantity == Decimal("1")

    async def test_fee_deducted_on_open(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(_fill(fee="5"), strategy_id=STRATEGY)

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        assert pos.realized_pnl == Decimal("-5")


class TestAddToPosition:
    async def test_add_recalculates_avg_price(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.BUY, quantity="1", price="40000", fee="0"),
            strategy_id=STRATEGY,
        )
        await tracker.update_on_fill(
            _fill(side=Side.BUY, quantity="1", price="42000", fee="0"),
            strategy_id=STRATEGY,
        )

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        assert pos.quantity == Decimal("2")
        # Weighted average: (40000 * 1 + 42000 * 1) / 2 = 41000
        assert pos.avg_entry_price == Decimal("41000")
        assert pos.direction == Direction.LONG


class TestReducePosition:
    async def test_partial_close_reduces_quantity(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.BUY, quantity="2", price="40000", fee="0"),
            strategy_id=STRATEGY,
        )
        # Sell 1 of 2
        await tracker.update_on_fill(
            _fill(side=Side.SELL, quantity="1", price="41000", fee="5"),
            strategy_id=STRATEGY,
        )

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        assert pos.quantity == Decimal("1")
        assert pos.direction == Direction.LONG
        # Realized: (41000 - 40000) * 1 - 5 fee = 995
        assert pos.realized_pnl == Decimal("995")


class TestClosePosition:
    async def test_full_close_sets_flat(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.BUY, quantity="1", price="40000", fee="0"),
            strategy_id=STRATEGY,
        )
        await tracker.update_on_fill(
            _fill(side=Side.SELL, quantity="1", price="42000", fee="10"),
            strategy_id=STRATEGY,
        )

        # Closed position should not appear in get_all_positions
        positions = await tracker.get_all_positions()
        assert len(positions) == 0

        # Direct key lookup still works but quantity is zero
        pos = tracker._positions.get((SYMBOL, EXCHANGE, STRATEGY))
        assert pos is not None
        assert pos.quantity == Decimal("0")
        assert pos.direction == Direction.FLAT
        # Realized: (42000 - 40000) * 1 - 10 fee = 1990
        assert pos.realized_pnl == Decimal("1990")

    async def test_close_short_position(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.SELL, quantity="1", price="40000", fee="0"),
            strategy_id=STRATEGY,
        )
        await tracker.update_on_fill(
            _fill(side=Side.BUY, quantity="1", price="38000", fee="0"),
            strategy_id=STRATEGY,
        )

        pos = tracker._positions.get((SYMBOL, EXCHANGE, STRATEGY))
        assert pos is not None
        assert pos.quantity == Decimal("0")
        # Realized: (40000 - 38000) * 1 = 2000
        assert pos.realized_pnl == Decimal("2000")


class TestUpdatePrice:
    async def test_update_price_updates_unrealized_pnl(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.BUY, quantity="2", price="40000", fee="0"),
            strategy_id=STRATEGY,
        )
        await tracker.update_price(SYMBOL, EXCHANGE, Decimal("41000"))

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        # Unrealized: (41000 - 40000) * 2 = 2000
        assert pos.unrealized_pnl == Decimal("2000")

    async def test_update_price_short_unrealized(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.SELL, quantity="3", price="50000", fee="0"),
            strategy_id=STRATEGY,
        )
        await tracker.update_price(SYMBOL, EXCHANGE, Decimal("48000"))

        pos = await tracker.get_position(SYMBOL, EXCHANGE)
        assert pos is not None
        # Unrealized: (50000 - 48000) * 3 = 6000
        assert pos.unrealized_pnl == Decimal("6000")


class TestFilterByExchange:
    async def test_get_positions_by_exchange(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.BUY, exchange_id="binance"),
            strategy_id=STRATEGY,
        )
        await tracker.update_on_fill(
            _fill(side=Side.BUY, exchange_id="bybit", symbol="ETHUSDT"),
            strategy_id=STRATEGY,
        )

        binance_positions = await tracker.get_positions_by_exchange("binance")
        assert len(binance_positions) == 1
        assert binance_positions[0].exchange_id == "binance"

        bybit_positions = await tracker.get_positions_by_exchange("bybit")
        assert len(bybit_positions) == 1
        assert bybit_positions[0].exchange_id == "bybit"

        kraken_positions = await tracker.get_positions_by_exchange("kraken")
        assert len(kraken_positions) == 0


class TestFilterByStrategy:
    async def test_get_positions_by_strategy(self) -> None:
        tracker = PositionTracker()
        await tracker.update_on_fill(
            _fill(side=Side.BUY, symbol="BTCUSDT"),
            strategy_id="alpha",
        )
        await tracker.update_on_fill(
            _fill(side=Side.BUY, symbol="ETHUSDT"),
            strategy_id="beta",
        )

        alpha = await tracker.get_positions_by_strategy("alpha")
        assert len(alpha) == 1
        assert alpha[0].strategy_id == "alpha"

        beta = await tracker.get_positions_by_strategy("beta")
        assert len(beta) == 1
        assert beta[0].strategy_id == "beta"

        gamma = await tracker.get_positions_by_strategy("gamma")
        assert len(gamma) == 0
