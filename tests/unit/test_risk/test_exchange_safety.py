"""Tests for ExchangeSafetyManager: safety order placement and cancellation."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hydra.core.types import Direction, Position, Symbol
from hydra.risk.exchange_safety import ExchangeSafetyManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_position(
    symbol: str = "BTCUSDT",
    direction: Direction = Direction.LONG,
    quantity: str = "0.1",
    entry_price: str = "42000",
    exchange_id: str = "binance",
) -> Position:
    return Position(
        symbol=Symbol(symbol),
        direction=direction,
        quantity=Decimal(quantity),
        avg_entry_price=Decimal(entry_price),
        unrealized_pnl=Decimal("0"),
        realized_pnl=Decimal("0"),
        strategy_id="test",
        exchange_id=exchange_id,
    )


def _mock_exchange_client() -> AsyncMock:
    client = AsyncMock()
    # Each create_order call returns a unique id
    call_count = 0

    async def _create_order(**kwargs: Any) -> dict[str, Any]:
        nonlocal call_count
        call_count += 1
        return {"id": f"safety-{call_count}"}

    client.create_order.side_effect = _create_order
    client.cancel_order.return_value = {"status": "CANCELLED"}
    return client


# ---------------------------------------------------------------------------
# Placement tests
# ---------------------------------------------------------------------------


class TestPlaceSafetyOrders:
    async def test_returns_order_ids(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position()
        client = _mock_exchange_client()

        ids = await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )
        assert len(ids) == 2  # normal stop + emergency stop
        assert all(isinstance(oid, str) for oid in ids)
        assert all(oid for oid in ids)

    async def test_normal_and_emergency_stops_placed(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position(entry_price="42000")
        client = _mock_exchange_client()

        await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )
        # Should have 2 create_order calls
        assert client.create_order.call_count == 2

    async def test_emergency_stop_distance_is_2x_normal(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position(direction=Direction.LONG, entry_price="42000")
        client = _mock_exchange_client()

        await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )

        calls = client.create_order.call_args_list
        assert len(calls) == 2

        # Normal stop: 42000 - 500 = 41500
        normal_call = calls[0]
        normal_stop_price = normal_call.kwargs.get("stop_price") or normal_call[1].get("stop_price")
        assert normal_stop_price == Decimal("41500")

        # Emergency stop: 42000 - 1000 = 41000
        emergency_call = calls[1]
        emergency_stop_price = emergency_call.kwargs.get("stop_price") or emergency_call[1].get(
            "stop_price"
        )
        assert emergency_stop_price == Decimal("41000")

    async def test_short_position_stops_above_entry(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position(direction=Direction.SHORT, entry_price="42000")
        client = _mock_exchange_client()

        await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )
        calls = client.create_order.call_args_list

        # Normal stop: 42000 + 500 = 42500
        normal_stop_price = calls[0].kwargs.get("stop_price") or calls[0][1].get("stop_price")
        assert normal_stop_price == Decimal("42500")

        # Emergency stop: 42000 + 1000 = 43000
        emergency_stop_price = calls[1].kwargs.get("stop_price") or calls[1][1].get("stop_price")
        assert emergency_stop_price == Decimal("43000")

    async def test_flat_position_no_orders(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position(direction=Direction.FLAT, quantity="0")
        client = _mock_exchange_client()

        ids = await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )
        assert len(ids) == 0
        client.create_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# Cancellation tests
# ---------------------------------------------------------------------------


class TestCancelSafetyOrders:
    async def test_cancel_removes_tracked_orders(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position()
        client = _mock_exchange_client()

        await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )
        # Should have tracked orders
        assert len(mgr._safety_orders) == 1

        await mgr.cancel_safety_orders(pos, client)
        # Should be removed from tracking
        assert len(mgr._safety_orders) == 0
        assert client.cancel_order.call_count == 2

    async def test_cancel_on_empty_is_noop(self) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position()
        client = _mock_exchange_client()

        # No safety orders placed, cancel should not fail
        await mgr.cancel_safety_orders(pos, client)
        client.cancel_order.assert_not_awaited()


# ---------------------------------------------------------------------------
# Exchange-specific behavior
# ---------------------------------------------------------------------------


class TestExchangeSpecific:
    @pytest.mark.parametrize(
        "exchange_id",
        ["binance", "bybit", "kraken", "okx"],
        ids=["binance", "bybit", "kraken", "okx"],
    )
    async def test_safety_orders_placed_for_all_exchanges(
        self,
        exchange_id: str,
    ) -> None:
        mgr = ExchangeSafetyManager()
        pos = _make_position(exchange_id=exchange_id)
        client = _mock_exchange_client()

        ids = await mgr.place_safety_orders(
            position=pos,
            exchange_client=client,
            stop_distance_atr=Decimal("500"),
        )
        assert len(ids) == 2
