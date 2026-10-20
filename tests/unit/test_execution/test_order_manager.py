"""Tests for OrderManager: submit flow, cancel, dedup, stale cleanup."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hydra.core.events import OrderCancelEvent, OrderFillEvent, RiskCheckResult
from hydra.core.types import (
    OHLCV,
    MarketType,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
)
from hydra.execution.order_manager import PROTECTIVE_TAG, OrderManager
from hydra.execution.paper_trading import PaperTradingExecutor

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(**overrides: Any) -> OrderRequest:
    defaults: dict[str, Any] = {
        "symbol": Symbol("BTCUSDT"),
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "quantity": Decimal("0.01"),
        "strategy_id": "test",
        "exchange_id": "binance",
        "market_type": MarketType.SPOT,
    }
    defaults.update(overrides)
    return OrderRequest(**defaults)


def _mock_executor(
    fill_immediately: bool = True,
    order_id: str = "ex-123",
) -> AsyncMock:
    executor = AsyncMock()
    if fill_immediately:
        executor.create_order.return_value = {
            "id": order_id,
            "status": "FILLED",
            "price": "42000.0",
            "filled": True,
            "fee": {"cost": "0.01", "currency": "USDT"},
        }
    else:
        executor.create_order.return_value = {
            "id": order_id,
            "status": "SUBMITTED",
            "filled": False,
        }
    executor.cancel_order.return_value = {"id": order_id, "status": "CANCELLED"}
    return executor


def _mock_event_bus() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSubmitOrder:
    async def test_submit_returns_order_id(self) -> None:
        executor = _mock_executor(fill_immediately=False, order_id="ord-1")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        order_id = await mgr.submit_order(_make_order())
        assert order_id == "ord-1"
        executor.create_order.assert_awaited_once()

    async def test_submit_with_immediate_fill_publishes_event(self) -> None:
        executor = _mock_executor(fill_immediately=True, order_id="ord-2")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        order_id = await mgr.submit_order(_make_order())
        assert order_id == "ord-2"
        bus.publish.assert_awaited_once()
        event = bus.publish.call_args[0][0]
        assert isinstance(event, OrderFillEvent)
        assert event.order_id == "ord-2"
        assert event.status == OrderStatus.FILLED

    async def test_submit_tracks_order_status(self) -> None:
        executor = _mock_executor(fill_immediately=False, order_id="ord-3")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        await mgr.submit_order(_make_order())
        assert mgr.get_order_status("ord-3") == OrderStatus.SUBMITTED

    async def test_filled_order_status(self) -> None:
        executor = _mock_executor(fill_immediately=True, order_id="ord-4")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        await mgr.submit_order(_make_order())
        assert mgr.get_order_status("ord-4") == OrderStatus.FILLED

    async def test_submit_with_risk_check_pass(self) -> None:
        executor = _mock_executor(fill_immediately=False)
        bus = _mock_event_bus()
        risk_checker = AsyncMock()
        risk_checker.check_order.return_value = RiskCheckResult(approved=True, reason="ok")

        mgr = OrderManager(
            executor=executor,
            event_bus=bus,
            risk_checker=risk_checker,
        )
        order_id = await mgr.submit_order(_make_order())
        assert order_id
        risk_checker.check_order.assert_awaited_once()

    async def test_submit_with_risk_check_fail(self) -> None:
        executor = _mock_executor(fill_immediately=False)
        bus = _mock_event_bus()
        risk_checker = AsyncMock()
        risk_checker.check_order.return_value = RiskCheckResult(
            approved=False, reason="position too large"
        )

        mgr = OrderManager(
            executor=executor,
            event_bus=bus,
            risk_checker=risk_checker,
        )
        with pytest.raises(ValueError, match="Risk check failed"):
            await mgr.submit_order(_make_order())
        executor.create_order.assert_not_awaited()


class TestCancelOrder:
    async def test_cancel_existing_order(self) -> None:
        executor = _mock_executor(fill_immediately=False, order_id="ord-c1")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        await mgr.submit_order(_make_order())
        ok = await mgr.cancel_order("ord-c1")
        assert ok is True
        assert mgr.get_order_status("ord-c1") == OrderStatus.CANCELLED

        bus.publish.assert_awaited_once()
        event = bus.publish.call_args[0][0]
        assert isinstance(event, OrderCancelEvent)

    async def test_cancel_nonexistent_order(self) -> None:
        executor = _mock_executor()
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        ok = await mgr.cancel_order("no-such-order")
        assert ok is False

    async def test_cancel_already_filled(self) -> None:
        executor = _mock_executor(fill_immediately=True, order_id="ord-filled")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        await mgr.submit_order(_make_order())
        ok = await mgr.cancel_order("ord-filled")
        assert ok is False


class TestGetOpenOrders:
    async def test_open_orders_returns_submitted(self) -> None:
        executor = _mock_executor(fill_immediately=False, order_id="ord-open")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        await mgr.submit_order(_make_order(symbol=Symbol("BTCUSDT")))
        open_orders = mgr.get_open_orders()
        assert len(open_orders) == 1
        assert open_orders[0].symbol == Symbol("BTCUSDT")

    async def test_open_orders_excludes_filled(self) -> None:
        executor = _mock_executor(fill_immediately=True, order_id="ord-f")
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        await mgr.submit_order(_make_order())
        open_orders = mgr.get_open_orders()
        assert len(open_orders) == 0

    async def test_open_orders_filter_by_symbol(self) -> None:
        executor = _mock_executor(fill_immediately=False)
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus)

        # We need different order_ids for dedup to pass, use different symbols
        executor.create_order.return_value = {"id": "o1", "status": "SUBMITTED", "filled": False}
        await mgr.submit_order(_make_order(symbol=Symbol("BTCUSDT")))

        executor.create_order.return_value = {"id": "o2", "status": "SUBMITTED", "filled": False}
        await mgr.submit_order(_make_order(symbol=Symbol("ETHUSDT")))

        btc_orders = mgr.get_open_orders(symbol="BTCUSDT")
        assert len(btc_orders) == 1
        assert btc_orders[0].symbol == Symbol("BTCUSDT")


class TestStaleCleanup:
    async def test_stale_orders_cancelled(self) -> None:
        executor = _mock_executor(fill_immediately=False, order_id="stale-1")
        bus = _mock_event_bus()
        mgr = OrderManager(
            executor=executor,
            event_bus=bus,
            stale_order_timeout=0.0,  # immediate timeout for testing
        )

        await mgr.submit_order(_make_order())
        cancelled = await mgr.cleanup_stale_orders()
        assert "stale-1" in cancelled

    async def test_fresh_orders_not_cancelled(self) -> None:
        executor = _mock_executor(fill_immediately=False, order_id="fresh-1")
        bus = _mock_event_bus()
        mgr = OrderManager(
            executor=executor,
            event_bus=bus,
            stale_order_timeout=9999.0,
        )

        await mgr.submit_order(_make_order())
        cancelled = await mgr.cleanup_stale_orders()
        assert len(cancelled) == 0


# ---------------------------------------------------------------------------
# External fills (paper resting orders) and the protective cleanup exemption
# ---------------------------------------------------------------------------


class _Bus:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)


def _flat(price: str, hour: int) -> OHLCV:
    p = Decimal(price)
    return OHLCV(
        open=p,
        high=p,
        low=p,
        close=p,
        volume=Decimal("1"),
        timestamp=datetime(2026, 10, 20, hour, tzinfo=UTC),
    )


class TestExternalFills:
    async def _long_with_stop(self) -> tuple[OrderManager, _Bus, PaperTradingExecutor, str]:
        ex = PaperTradingExecutor(initial_balances={"USDT": Decimal("1000")})
        bus = _Bus()
        om = OrderManager(ex, bus, session_id="s1")
        await ex.on_closed_bar("BTCUSDT", _flat("100", 1))
        await om.submit_order(_make_order(quantity=Decimal("1")))
        stop_id = await om.submit_order(
            _make_order(
                side=Side.SELL,
                order_type=OrderType.STOP_MARKET,
                quantity=Decimal("1"),
                stop_price=Decimal("95"),
                reduce_only=True,
                tag=PROTECTIVE_TAG,
            )
        )
        assert stop_id is not None
        return om, bus, ex, stop_id

    async def test_resting_fill_is_published_with_net_pnl_and_marked_filled(self) -> None:
        om, bus, ex, stop_id = await self._long_with_stop()
        fills = await ex.on_closed_bar("BTCUSDT", _flat("90", 2))
        (event,) = [e for f in fills if (e := await om.on_external_fill(f)) is not None]
        assert om.get_order_status(stop_id) == OrderStatus.FILLED
        assert (event.order_id, event.side, event.price) == (stop_id, Side.SELL, Decimal("89.910"))
        assert event.session_id == "s1" and event.tag == PROTECTIVE_TAG and event.reduce_only
        # 89.91 - 100.1 gross, minus exit fee 0.08991 and entry fee 0.1001
        assert event.gross_pnl == Decimal("-10.190")
        assert event.realized_pnl == Decimal("-10.38001")
        assert bus.events[-1] is event

    async def test_cancelled_record_publishes_nothing(self) -> None:
        ex = PaperTradingExecutor(initial_balances={"USDT": Decimal("100")})
        bus = _Bus()
        om = OrderManager(ex, bus, session_id="s1")
        await ex.on_closed_bar("BTCUSDT", _flat("100", 1))
        oid = await om.submit_order(
            _make_order(
                order_type=OrderType.STOP_MARKET, quantity=Decimal("0.9"), stop_price=Decimal("105")
            )
        )
        assert oid is not None
        # gaps to 200: the resting BUY cannot be paid for and is dropped
        fills = await ex.on_closed_bar("BTCUSDT", _flat("200", 2))
        assert [f.status for f in fills] == ["CANCELLED"]
        assert await om.on_external_fill(fills[0]) is None
        assert om.get_order_status(oid) == OrderStatus.CANCELLED
        assert not [e for e in bus.events if isinstance(e, OrderFillEvent) and e.order_id == oid]

    async def test_stale_cleanup_skips_protective_orders(self) -> None:
        om, _, ex, stop_id = await self._long_with_stop()
        limit_id = await om.submit_order(
            _make_order(order_type=OrderType.LIMIT, quantity=Decimal("2"), price=Decimal("50"))
        )
        om._stale_order_timeout = 0.0
        assert await om.cleanup_stale_orders() == [limit_id]
        assert om.get_order_status(stop_id) == OrderStatus.SUBMITTED
        assert [o["id"] for o in await ex.fetch_open_orders()] == [stop_id]
