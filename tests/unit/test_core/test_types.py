"""Tests for hydra.core.types — value objects, enums, and dataclasses."""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.types import (
    OHLCV,
    Direction,
    MarketType,
    OrderFill,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Symbol,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Symbol
# ---------------------------------------------------------------------------


class TestSymbol:
    def test_create(self) -> None:
        s = Symbol("BTCUSDT")
        assert s == "BTCUSDT"
        assert isinstance(s, str)

    def test_comparison(self) -> None:
        assert Symbol("BTCUSDT") == Symbol("BTCUSDT")
        assert Symbol("BTCUSDT") != Symbol("ETHUSDT")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class TestTimeframe:
    def test_values(self) -> None:
        assert Timeframe.M1.value == "1m"
        assert Timeframe.M5.value == "5m"
        assert Timeframe.M15.value == "15m"
        assert Timeframe.H1.value == "1h"
        assert Timeframe.H4.value == "4h"
        assert Timeframe.D1.value == "1d"
        assert Timeframe.W1.value == "1w"

    def test_from_value(self) -> None:
        assert Timeframe("1h") is Timeframe.H1

    def test_all_members(self) -> None:
        assert len(Timeframe) == 7


class TestSide:
    def test_buy_sell(self) -> None:
        assert Side.BUY.value == "BUY"
        assert Side.SELL.value == "SELL"

    def test_string_access(self) -> None:
        assert Side("BUY") is Side.BUY


class TestOrderType:
    def test_all_types(self) -> None:
        expected = {
            "MARKET",
            "LIMIT",
            "STOP_MARKET",
            "STOP_LIMIT",
            "TAKE_PROFIT_MARKET",
            "OCO",
            "TRAILING_STOP",
        }
        assert {ot.value for ot in OrderType} == expected

    def test_count(self) -> None:
        assert len(OrderType) == 7


class TestOrderStatus:
    def test_all_statuses(self) -> None:
        expected = {
            "PENDING",
            "SUBMITTED",
            "PARTIALLY_FILLED",
            "FILLED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
        }
        assert {os.value for os in OrderStatus} == expected

    def test_count(self) -> None:
        assert len(OrderStatus) == 7


class TestDirection:
    def test_values(self) -> None:
        assert Direction.LONG.value == "LONG"
        assert Direction.SHORT.value == "SHORT"
        assert Direction.FLAT.value == "FLAT"


class TestMarketType:
    def test_values(self) -> None:
        assert MarketType.SPOT.value == "SPOT"
        assert MarketType.FUTURES.value == "FUTURES"


# ---------------------------------------------------------------------------
# OHLCV — frozen dataclass
# ---------------------------------------------------------------------------


class TestOHLCV:
    def test_create(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        bar = OHLCV(
            open=Decimal("42000.50"),
            high=Decimal("42500.00"),
            low=Decimal("41800.25"),
            close=Decimal("42100.00"),
            volume=Decimal("123.456"),
            timestamp=ts,
        )
        assert bar.open == Decimal("42000.50")
        assert bar.high == Decimal("42500.00")
        assert bar.low == Decimal("41800.25")
        assert bar.close == Decimal("42100.00")
        assert bar.volume == Decimal("123.456")
        assert bar.timestamp == ts

    def test_frozen(self) -> None:
        bar = OHLCV(
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("0.5"),
            close=Decimal("1.5"),
            volume=Decimal("100"),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        with pytest.raises(FrozenInstanceError):
            bar.open = Decimal("999")  # type: ignore[misc]

    def test_decimal_precision(self) -> None:
        bar = OHLCV(
            open=Decimal("0.00000001"),
            high=Decimal("0.00000002"),
            low=Decimal("0.00000001"),
            close=Decimal("0.00000002"),
            volume=Decimal("0.00000001"),
            timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert bar.open == Decimal("0.00000001")

    def test_naive_timestamp_gets_utc(self) -> None:
        bar = OHLCV(
            open=Decimal("1"),
            high=Decimal("2"),
            low=Decimal("0.5"),
            close=Decimal("1.5"),
            volume=Decimal("100"),
            timestamp=datetime(2024, 1, 1),
        )
        assert bar.timestamp.tzinfo == UTC

    def test_equality(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        bar1 = OHLCV(Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("100"), ts)
        bar2 = OHLCV(Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("100"), ts)
        assert bar1 == bar2

    def test_hash(self) -> None:
        ts = datetime(2024, 1, 1, tzinfo=UTC)
        bar = OHLCV(Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("100"), ts)
        # Frozen dataclasses are hashable
        assert hash(bar) is not None
        s = {bar}
        assert bar in s


# ---------------------------------------------------------------------------
# OrderRequest — mutable dataclass
# ---------------------------------------------------------------------------


class TestOrderRequest:
    def test_create_minimal(self) -> None:
        req = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            strategy_id="momentum_v1",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )
        assert req.symbol == "BTCUSDT"
        assert req.side == Side.BUY
        assert req.order_type == OrderType.MARKET
        assert req.quantity == Decimal("0.01")
        assert req.price is None
        assert req.stop_price is None
        assert req.request_id  # auto-generated UUID

    def test_create_limit_with_price(self) -> None:
        req = OrderRequest(
            symbol=Symbol("ETHUSDT"),
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=Decimal("1.5"),
            strategy_id="mean_revert",
            exchange_id="bybit",
            market_type=MarketType.FUTURES,
            price=Decimal("3500.00"),
        )
        assert req.price == Decimal("3500.00")
        assert req.exchange_id == "bybit"
        assert req.market_type == MarketType.FUTURES

    def test_mutable(self) -> None:
        req = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            strategy_id="test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )
        req.quantity = Decimal("0.02")
        assert req.quantity == Decimal("0.02")

    def test_unique_request_ids(self) -> None:
        req1 = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            strategy_id="test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )
        req2 = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            strategy_id="test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )
        assert req1.request_id != req2.request_id


# ---------------------------------------------------------------------------
# OrderFill
# ---------------------------------------------------------------------------


class TestOrderFill:
    def test_create(self) -> None:
        ts = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
        fill = OrderFill(
            order_id="ord-123",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("65000.00"),
            fee=Decimal("0.001"),
            fee_currency="BTC",
            timestamp=ts,
            exchange_id="binance",
        )
        assert fill.order_id == "ord-123"
        assert fill.fee == Decimal("0.001")
        assert fill.fee_currency == "BTC"
        assert fill.timestamp == ts


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------


class TestPosition:
    def test_create(self) -> None:
        pos = Position(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            quantity=Decimal("0.1"),
            avg_entry_price=Decimal("60000"),
            unrealized_pnl=Decimal("500"),
            realized_pnl=Decimal("0"),
            strategy_id="trend_follow",
            exchange_id="binance",
        )
        assert pos.direction == Direction.LONG
        assert pos.unrealized_pnl == Decimal("500")

    def test_mutable(self) -> None:
        pos = Position(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            quantity=Decimal("0.1"),
            avg_entry_price=Decimal("60000"),
            unrealized_pnl=Decimal("500"),
            realized_pnl=Decimal("0"),
            strategy_id="trend_follow",
            exchange_id="binance",
        )
        pos.unrealized_pnl = Decimal("1000")
        assert pos.unrealized_pnl == Decimal("1000")

    def test_flat_position(self) -> None:
        pos = Position(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.FLAT,
            quantity=Decimal("0"),
            avg_entry_price=Decimal("0"),
            unrealized_pnl=Decimal("0"),
            realized_pnl=Decimal("150.50"),
            strategy_id="scalper",
            exchange_id="kraken",
        )
        assert pos.direction == Direction.FLAT
        assert pos.quantity == Decimal("0")
        assert pos.realized_pnl == Decimal("150.50")
