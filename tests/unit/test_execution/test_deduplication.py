"""Parameterized deduplication tests for OrderManager.

Verifies that the same symbol+side+quantity within 1s is rejected,
and that after the dedup window expires the order is allowed.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest

from hydra.core.types import (
    MarketType,
    OrderRequest,
    OrderType,
    Side,
    Symbol,
)
from hydra.execution.order_manager import OrderManager

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


def _mock_executor(order_id: str = "ex-1") -> AsyncMock:
    executor = AsyncMock()
    executor.create_order.return_value = {
        "id": order_id,
        "status": "SUBMITTED",
        "filled": False,
    }
    return executor


def _mock_event_bus() -> AsyncMock:
    return AsyncMock()


# ---------------------------------------------------------------------------
# Parameterized tests
# ---------------------------------------------------------------------------


class TestDeduplication:
    @pytest.mark.parametrize(
        ("description", "second_order_overrides", "expect_rejected"),
        [
            (
                "same_symbol_side_qty_within_window",
                {},
                True,
            ),
            (
                "different_symbol_within_window",
                {"symbol": Symbol("ETHUSDT")},
                False,
            ),
            (
                "different_side_within_window",
                {"side": Side.SELL},
                False,
            ),
            (
                "different_quantity_within_window",
                {"quantity": Decimal("0.02")},
                False,
            ),
        ],
        ids=[
            "same-order-rejected",
            "different-symbol-allowed",
            "different-side-allowed",
            "different-qty-allowed",
        ],
    )
    async def test_dedup_within_window(
        self,
        description: str,
        second_order_overrides: dict[str, Any],
        expect_rejected: bool,
    ) -> None:
        executor = _mock_executor()
        bus = _mock_event_bus()
        mgr = OrderManager(executor=executor, event_bus=bus, dedup_window=1.0)

        # First order always succeeds
        await mgr.submit_order(_make_order())

        if expect_rejected:
            with pytest.raises(ValueError, match="Duplicate order rejected"):
                await mgr.submit_order(_make_order(**second_order_overrides))
        else:
            order_id = await mgr.submit_order(_make_order(**second_order_overrides))
            assert order_id

    async def test_same_order_allowed_after_window(self) -> None:
        """After the dedup window expires, the same order should be accepted."""
        executor = _mock_executor()
        bus = _mock_event_bus()
        # Very short dedup window so we don't need real sleep
        mgr = OrderManager(executor=executor, event_bus=bus, dedup_window=0.0)

        await mgr.submit_order(_make_order())
        # With window=0.0 the next call should always succeed
        order_id = await mgr.submit_order(_make_order())
        assert order_id
