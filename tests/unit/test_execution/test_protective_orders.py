"""Mandatory protective exits (decision 17) on the real session seam.

Entry: 1 BTC at 100 x 1.001 = 100.1, fee 0.1001, USDT 899.7999. Stops evaluate on the next closed
bar with P1-D's rules (worse of stop and open, then 0.1 % slippage; take-profit at the better of
limit and open, maker).

The ``TestLiveGroups`` section pins the live contracts (P3-B): a live executor's create
responses carry ``legs``/``oco_link`` (the orderListId convention), fills arrive per leg, and
adopt consumes ``normalize_open_orders`` output.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import ExitSignal, OrderFillEvent
from hydra.core.types import (
    Direction,
    MarketType,
    OrderStatus,
    Position,
    Side,
    Symbol,
)
from hydra.execution.exchange_stops import normalize_open_orders
from hydra.execution.order_manager import PROTECTIVE_TAG, OrderManager
from hydra.execution.paper_trading import OrderRejectedError
from hydra.execution.protective_orders import (
    ProtectionSpec,
    ProtectiveOrderManager,
    default_stop,
    spec_from_policy,
)
from hydra.strategy.config import StrategyRiskConfig
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    held,
    rule,
    settle,
    usdt,
    write_strategy,
)

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal
ENTRY_AT_100 = rule(("less_than", 100.5), ("greater_than", 99.5))
EXIT_ABOVE_105 = rule(("greater_than", 105))


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


async def _open(session: Any) -> list[tuple[str, str, float, Any, Any]]:
    return [
        (o["side"], o["type"], o["amount"], o["stop_price"], o["price"])
        for o in await executor(session).fetch_open_orders("BTCUSDT")
    ]


async def _entered(h: Harness, strategies_dir: Path, sid: str, **kw: Any) -> Any:
    session = await h.session(write_strategy(strategies_dir, sid, entry_long=ENTRY_AT_100, **kw))
    await h.bar(session, "100")
    assert await held(session) == D("1.00000")
    return session


class TestArming:
    async def test_every_entry_gets_the_default_five_percent_stop(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "default")
        assert await _open(session) == [("SELL", "STOP_MARKET", 1.0, 95.095, None)]
        group = session._runtime.protect.armed("BTCUSDT")
        assert group is not None
        om = session._order_manager
        assert om.get_order_status(group.order_id) == OrderStatus.SUBMITTED
        assert om._orders[group.order_id].request.tag == PROTECTIVE_TAG
        assert om._orders[group.order_id].request.reduce_only

    async def test_protective_orders_are_never_cleaned_up_as_stale(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "stale")
        om = session._order_manager
        om._stale_order_timeout = 0.0
        assert await om.cleanup_stale_orders() == []
        assert await _open(session) == [("SELL", "STOP_MARKET", 1.0, 95.095, None)]

    async def test_trailing_stop_from_the_risk_block(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(
            h,
            strategies_dir,
            "trail",
            risk={"stop_loss_method": "trailing_stop", "stop_loss_value": 3.0},
        )
        orders = await executor(session).fetch_open_orders("BTCUSDT")
        assert [(o["type"], o["amount"], o["trail_pct"]) for o in orders] == [
            ("TRAILING_STOP", 1.0, 0.03)
        ]
        await h.bar(session, "110", open="100", high="110", low="100")
        await h.bar(session, "105", open="110", high="110", low="105")
        # peak 110 -> trigger 106.7, hit on the way down; SELL slipped 0.1 %
        assert executor(session).get_fills()[-1].price == D("106.7") * D("0.999")
        assert await held(session) == 0


class TestStopFills:
    async def test_protective_stop_fires_on_a_closed_bar(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(
            h,
            strategies_dir,
            "stop2",
            risk={"stop_loss_method": "fixed_pct", "stop_loss_value": 2.0},
        )
        assert await _open(session) == [("SELL", "STOP_MARKET", 1.0, 98.098, None)]
        seen: list[OrderFillEvent] = []

        async def record(event: Any) -> None:
            seen.append(event)

        await session._runtime.bus.subscribe("order_fill", record)
        await h.bar(session, "102", open="100", high="103", low="98")
        fill = executor(session).get_fills()[-1]
        assert (fill.side, fill.price, fill.reason) == ("SELL", D("97.999902"), "stop_loss")
        assert fill.net_pnl == D("-2.298197902")
        assert await usdt(session) == D("997.701802098")
        assert session._runtime.context.position("BTCUSDT") is None
        assert await _open(session) == []
        assert [e.realized_pnl for e in seen] == [D("-2.298197902")]
        assert session._runtime.protect.armed("BTCUSDT") is None
        assert session._runtime.risk.account().consecutive_losses == 1

    async def test_gap_through_the_stop_fills_at_the_open(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(
            h,
            strategies_dir,
            "gap",
            risk={"stop_loss_method": "fixed_pct", "stop_loss_value": 2.0},
        )
        await h.bar(session, "97.2", open="97", high="97.5", low="96")
        assert executor(session).get_fills()[-1].price == D("97") * D("0.999")

    async def test_strategy_exit_cancels_the_stop_and_no_short_follows(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "exitc", exit_long=EXIT_ABOVE_105)
        await h.bar(session, "110")
        assert [f.side for f in executor(session).get_fills()] == ["BUY", "SELL"]
        assert await _open(session) == []
        balance = await usdt(session)
        await h.bar(session, "90", open="110", high="110", low="90")
        assert [f.side for f in executor(session).get_fills()] == ["BUY", "SELL"]
        assert await held(session) == 0
        assert await usdt(session) == balance == D("1009.58001")

    async def test_oco_take_profit_cancels_the_stop(self, h: Harness, strategies_dir: Path) -> None:
        session = await _entered(
            h,
            strategies_dir,
            "oco",
            risk={
                "stop_loss_method": "fixed_pct",
                "stop_loss_value": 2.0,
                "take_profit_method": "fixed_pct",
                "take_profit_value": 5.0,
            },
        )
        legs = sorted(await _open(session), key=lambda o: o[1])
        assert legs == [
            ("SELL", "LIMIT", 1.0, None, 105.105),
            ("SELL", "STOP_MARKET", 1.0, 98.098, None),
        ]
        await h.bar(session, "104", open="100", high="105.2", low="99.5")
        fill = executor(session).get_fills()[-1]
        assert (fill.price, fill.liquidity, fill.reason) == (D("105.105"), "maker", "take_profit")
        assert await _open(session) == []
        assert await held(session) == 0

    async def test_partial_exit_rearms_the_rest_at_the_same_level(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "partial")
        await session._runtime.bus.publish(
            ExitSignal(symbol=Symbol("BTCUSDT"), strategy_id="partial", close_pct=0.5)
        )
        assert await held(session) == D("0.50000")
        assert await _open(session) == [("SELL", "STOP_MARKET", 0.5, 95.095, None)]


async def _failure_handled() -> None:
    from hydra.execution import session_wiring

    await asyncio.gather(*list(session_wiring._BACKGROUND))
    await settle()


@pytest.fixture()
def broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from hydra.core.websocket import manager

    sent: list[dict[str, Any]] = []

    async def broadcast(channel: str, payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(manager, "broadcast", broadcast)
    return sent


class TestUnprotected:
    async def test_exit_on_a_halted_ledger_fails_the_session_instead_of_dropping_the_stop(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await _entered(h, strategies_dir, "halt-exit")
        assert await _open(session) == [("SELL", "STOP_MARKET", 1.0, 95.095, None)]
        executor(session)._halt("second writer")
        await session._runtime.bus.publish(
            ExitSignal(symbol=Symbol("BTCUSDT"), strategy_id="halt-exit")
        )
        await _failure_handled()
        assert session.status == "error" and "halted" in (session.error_message or "")
        assert not session.task_alive
        # closed by the executor's own flatten at the mark: 899.7999 + 99.9 - 0.0999
        assert await held(session) == 0 and await usdt(session) == D("999.6000")
        assert (session.positions_at_stop, session.flatten_incomplete) == (0, False)

    async def test_halt_while_the_entry_fill_persists_fails_the_session(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await h.session(
            write_strategy(strategies_dir, "halt-entry", entry_long=ENTRY_AT_100)
        )
        ex = executor(session)
        real_persist = ex._persist

        async def second_writer_on_the_fill(*args: Any, **kwargs: Any) -> None:
            await real_persist(*args, **kwargs)
            if ex.get_fills() and ex.get_account_snapshot().halted is None:
                ex._halt("second writer")

        monkeypatch.setattr(ex, "_persist", second_writer_on_the_fill)
        await h.bar(session, "100")
        await _failure_handled()
        assert session.status == "error" and "halted" in (session.error_message or "")
        assert [f.side for f in ex.get_fills()] == ["BUY", "SELL"]
        assert await held(session) == 0

    async def test_rejected_stop_and_fallback_close_the_position(
        self,
        h: Harness,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
        broadcasts: list[dict[str, Any]],
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "nostop", entry_long=ENTRY_AT_100))
        ex = executor(session)
        real = ex.create_order

        async def no_resting_sells(*args: Any, **kwargs: Any) -> Any:
            if (kwargs.get("side"), kwargs.get("order_type")) in (
                ("SELL", "STOP_MARKET"),
                ("SELL", "OCO"),
            ):
                raise OrderRejectedError("venue refuses stops")
            return await real(*args, **kwargs)

        monkeypatch.setattr(ex, "create_order", no_resting_sells)
        await h.bar(session, "100")
        await settle()
        assert session.status == "running"
        assert [(f.side, f.price) for f in ex.get_fills()] == [
            ("BUY", D("100.100")),
            ("SELL", D("99.900")),
        ]
        assert await held(session) == 0 and await usdt(session) == D("999.6000")
        failed = [b for b in broadcasts if b.get("event") == "protection_failed"]
        assert [(b["symbol"], b["quantity"]) for b in failed] == [("BTCUSDT", "1.00000")]
        assert h.mgr is not None
        health = (await h.mgr.health_snapshot()).sessions[0]
        # the policy stop is the 5 % default, so there is no separate fallback attempt
        assert len(health.protection_failures) == 1
        assert "95.09500" in health.protection_failures[0]
        assert "venue refuses stops" in health.protection_failures[0]
        detail = await h.mgr.get_session_detail(session.session_id)
        assert detail["protection_failures"] == list(health.protection_failures)


class TestManualProtection:
    async def test_quick_trade_levels_become_an_oco_on_the_entry_fill(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        assert h.mgr is not None
        _, result = await h.mgr.submit_manual_order(
            symbol="BTCUSDT",
            side=Side.BUY,
            quantity=D("0.5"),
            protection=ProtectionSpec(stop_loss=D("90"), take_profit=D("120")),
        )
        assert result.status == "FILLED" and result.protection == "armed"
        legs = sorted(await _open(session), key=lambda o: o[1])
        assert legs == [
            ("SELL", "LIMIT", 0.5, None, 120.0),
            ("SELL", "STOP_MARKET", 0.5, 90.0, None),
        ]


class TestPolicyMath:
    def test_pct_stop_and_take_profit(self) -> None:
        cfg = StrategyRiskConfig(
            stop_loss_method="fixed_pct",
            stop_loss_value=2.0,
            take_profit_method="fixed_pct",
            take_profit_value=5.0,
        )
        assert spec_from_policy(cfg, D("100.1"), None) == ProtectionSpec(
            stop_loss=D("98.098"), take_profit=D("105.105")
        )

    def test_atr_levels_and_the_default_without_atr(self) -> None:
        cfg = StrategyRiskConfig(
            stop_loss_method="atr",
            stop_loss_value=1.5,
            take_profit_method="atr",
            take_profit_value=3.0,
        )
        assert spec_from_policy(cfg, D("100"), D("2")) == ProtectionSpec(
            stop_loss=D("97.0"), take_profit=D("106.0")
        )
        assert spec_from_policy(cfg, D("100"), None) == default_stop(D("100"))
        assert default_stop(D("100")).stop_loss == D("95.00")

    def test_invalid_combinations_are_refused(self) -> None:
        with pytest.raises(ValueError, match="trailing"):
            StrategyRiskConfig(
                stop_loss_method="trailing_stop",
                stop_loss_value=3.0,
                take_profit_method="fixed_pct",
                take_profit_value=5.0,
            )
        with pytest.raises(ValueError, match="below take_profit"):
            ProtectionSpec(stop_loss=D("110"), take_profit=D("100"))
        with pytest.raises(ValueError, match="mutually exclusive"):
            ProtectionSpec(stop_loss=D("90"), trailing_pct=D("0.02"))


# ---------------------------------------------------------------------------
# Live groups: the exchange_stops contracts through the real OrderManager
# ---------------------------------------------------------------------------


def _position(symbol: str, qty: str, entry: str = "100") -> Position:
    return Position(
        symbol=Symbol(symbol),
        direction=Direction.LONG,
        quantity=D(qty),
        avg_entry_price=D(entry),
        unrealized_pnl=D("0"),
        realized_pnl=D("0"),
        strategy_id="live",
        exchange_id="binance",
    )


class FakeLiveExecutor:
    """A live executor's seam behaviour: paper-shaped responses that carry ``legs``.

    OCO responses use the orderListId as ``id``/``oco_link`` and list their legs; the same
    convention the keyed client's ``create_oco`` returns (P3-A's executor delegates to
    ``exchange_stops`` and returns exactly this).
    """

    def __init__(
        self,
        positions: list[Any] | None = None,
        resting: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self.resting = resting or []
        self._positions = positions or []
        self._seq = 100

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "symbol": symbol,
                "side": side,
                "order_type": str(order_type),
                "quantity": D(str(quantity)),
                "price": price,
                "stop_price": stop_price,
                "params": dict(params or {}),
            }
        )
        otype = str(order_type)
        if otype == "MARKET":
            return {
                "id": f"m{uuid.uuid4().hex[:6]}",
                "status": "FILLED",
                "filled": True,
                "price": float(price or 99),
            }
        if otype == "OCO":
            self._seq += 1
            list_id = str(self._seq)
            legs = [
                {
                    "id": f"{list_id}-tp",
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "LIMIT",
                    "amount": float(quantity),
                    "price": float(price or 0),
                    "stop_price": None,
                    "status": "PENDING",
                    "filled": False,
                    "reason": "take_profit",
                    "oco_id": list_id,
                    "trail_pct": None,
                },
                {
                    "id": f"{list_id}-sl",
                    "symbol": symbol,
                    "side": "SELL",
                    "type": "STOP_MARKET",
                    "amount": float(quantity),
                    "price": None,
                    "stop_price": float(stop_price or 0),
                    "status": "PENDING",
                    "filled": False,
                    "reason": "stop_loss",
                    "oco_id": list_id,
                    "trail_pct": None,
                },
            ]
            return {
                "id": list_id,
                "symbol": symbol,
                "side": side,
                "type": "OCO",
                "amount": float(quantity),
                "price": float(price or 0),
                "stop_price": float(stop_price or 0),
                "status": "NEW",
                "filled": False,
                "oco_link": list_id,
                "legs": legs,
            }
        self._seq += 1
        order_id = str(self._seq)
        return {
            "id": order_id,
            "symbol": symbol,
            "side": side,
            "type": otype,
            "amount": float(quantity),
            "price": None,
            "stop_price": float(stop_price or 0) if stop_price is not None else None,
            "status": "NEW",
            "filled": False,
            "legs": [
                {
                    "id": order_id,
                    "symbol": symbol,
                    "side": "SELL",
                    "type": otype,
                    "amount": float(quantity),
                    "price": None,
                    "stop_price": float(stop_price or 0) if stop_price is not None else None,
                    "status": "PENDING",
                    "filled": False,
                    "reason": "trailing_stop" if otype == "TRAILING_STOP" else "stop_loss",
                    "oco_id": None,
                    "trail_pct": float(D(str((params or {}).get("trail_pct", "0")))),
                }
            ],
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        self.calls.append({"cancel_order": order_id, "symbol": symbol})
        return {"id": order_id, "status": "CANCELED"}

    async def get_positions(self, symbol: str | None = None) -> list[Any]:
        return [p for p in self._positions if symbol is None or str(p.symbol) == symbol]

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        return [o for o in self.resting if symbol is None or o["symbol"] == symbol]


def _manager(
    executor: FakeLiveExecutor, policy: StrategyRiskConfig | None = None
) -> ProtectiveOrderManager:
    om = OrderManager(executor, InMemoryEventBus())
    return ProtectiveOrderManager(
        om,
        executor,
        strategy_id="live",
        exchange_id="binance",
        market_type=MarketType.SPOT,
        policy=policy or StrategyRiskConfig(),
        atr=lambda symbol: None,
    )


def _fill(
    symbol: str,
    order_id: str,
    side: Side = Side.SELL,
    qty: str = "0.01",
    tag: str = "protective",
) -> OrderFillEvent:
    return OrderFillEvent(
        order_id=order_id,
        symbol=Symbol(symbol),
        side=side,
        quantity=D(qty),
        price=D("99"),
        tag=tag,
    )


class TestLiveGroups:
    OCO_POLICY = StrategyRiskConfig(
        stop_loss_method="fixed_pct",
        stop_loss_value=2.0,
        take_profit_method="fixed_pct",
        take_profit_value=3.0,
    )

    async def test_an_entry_fill_arms_a_confirmed_group_with_leg_ids(self) -> None:
        ex = FakeLiveExecutor([_position("BTCUSDT", "0.01")])
        protect = _manager(ex, self.OCO_POLICY)

        await protect.on_fill(_fill("BTCUSDT", "e1", side=Side.BUY, tag="entry"))

        group = protect.armed("BTCUSDT")
        assert group is not None
        assert group.order_id == "101"  # the orderListId, not a leg id
        assert group.order_type.value == "OCO"
        assert group.leg_ids == ("101-tp", "101-sl")
        assert group.confirmed is True
        assert group.quantity == D("0.01")
        assert group.spec == ProtectionSpec(stop_loss=D("98"), take_profit=D("103"))

    async def test_a_leg_fill_closes_the_group_by_parent_or_leg_id(self) -> None:
        for fill_id in ("101", "101-sl"):  # the parent convention, and the leg variant
            ex = FakeLiveExecutor([_position("BTCUSDT", "0.01")])
            protect = _manager(ex, self.OCO_POLICY)
            await protect.on_fill(_fill("BTCUSDT", "e1", side=Side.BUY, tag="entry"))
            assert protect.armed("BTCUSDT") is not None
            ex._positions = []  # the leg fill closed the position

            await protect.on_fill(_fill("BTCUSDT", fill_id))

            assert protect.armed("BTCUSDT") is None

    async def test_a_partial_leg_fill_rearms_the_remainder(self) -> None:
        ex = FakeLiveExecutor([_position("BTCUSDT", "0.01")])
        protect = _manager(ex, self.OCO_POLICY)
        await protect.on_fill(_fill("BTCUSDT", "e1", side=Side.BUY, tag="entry"))
        assert len(ex.calls) == 1

        ex._positions = [_position("BTCUSDT", "0.004")]
        await protect.on_fill(_fill("BTCUSDT", "101-tp", qty="0.006"))

        assert len(ex.calls) == 2
        assert ex.calls[1]["order_type"] == "OCO"
        assert ex.calls[1]["quantity"] == D("0.004")
        group = protect.armed("BTCUSDT")
        assert group is not None and group.confirmed and group.quantity == D("0.004")

    async def test_disarm_cancels_by_list_id_and_a_failed_exit_replaces(self) -> None:
        ex = FakeLiveExecutor([_position("BTCUSDT", "0.01")])
        protect = _manager(ex, self.OCO_POLICY)
        await protect.on_fill(_fill("BTCUSDT", "e1", side=Side.BUY, tag="entry"))

        await protect.disarm("BTCUSDT")

        assert protect.armed("BTCUSDT") is None
        assert ex.calls[-1] == {"cancel_order": "101", "symbol": "BTCUSDT"}

        await protect.rearm("BTCUSDT")

        group = protect.armed("BTCUSDT")
        assert group is not None and group.order_id != "101"  # a NEW OCO (cancel/replace)
        assert ex.calls[-1]["order_type"] == "OCO"

    async def test_adopt_attaches_normalized_legs_and_heals_a_gap(self) -> None:
        resting = normalize_open_orders(
            [
                {
                    "info": {
                        "symbol": "BTCUSDT",
                        "orderId": 11,
                        "orderListId": "5",
                        "type": "LIMIT_MAKER",
                        "side": "SELL",
                        "price": "103.00000000",
                        "origQty": "0.01000000",
                        "stopPrice": "0.00000000",
                        "trailingDelta": "0",
                        "status": "NEW",
                    }
                },
                {
                    "info": {
                        "symbol": "BTCUSDT",
                        "orderId": 12,
                        "orderListId": "5",
                        "type": "STOP_LOSS",
                        "side": "SELL",
                        "price": "0.00000000",
                        "origQty": "0.01000000",
                        "stopPrice": "98.00000000",
                        "trailingDelta": "0",
                        "status": "NEW",
                    }
                },
            ]
        )
        ex = FakeLiveExecutor([_position("BTCUSDT", "0.01")], resting=resting)
        protect = _manager(ex, self.OCO_POLICY)

        await protect.adopt(["BTCUSDT"])

        group = protect.armed("BTCUSDT")
        assert group is not None
        assert group.order_id == "5" and group.order_type.value == "OCO"
        assert group.spec == ProtectionSpec(stop_loss=D("98.0"), take_profit=D("103.0"))
        assert ex.calls == []  # attached, nothing placed

        ex.resting = []
        ex._positions = [_position("ETHUSDT", "0.01")]
        await protect.adopt(["ETHUSDT"])

        assert len(ex.calls) == 1 and ex.calls[0]["order_type"] == "OCO"  # the gap heal
        assert protect.armed("ETHUSDT") is not None

    async def test_every_placement_rejected_leaves_failures_and_no_group(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ex = FakeLiveExecutor([_position("BTCUSDT", "0.01")])
        real = ex.create_order

        async def refuse_resting_sells(*args: Any, **kwargs: Any) -> Any:
            if kwargs.get("side") == "SELL" and kwargs.get("order_type") != "MARKET":
                raise OrderRejectedError("venue refuses stops")
            return await real(*args, **kwargs)

        monkeypatch.setattr(ex, "create_order", refuse_resting_sells)
        protect = _manager(ex)

        await protect.on_fill(_fill("BTCUSDT", "e1", side=Side.BUY, tag="entry"))

        assert protect.armed("BTCUSDT") is None
        # the policy stop is the 5% default, so there is no separate fallback attempt
        assert len(protect.failures) == 1
        assert "venue refuses stops" in protect.failures[0]
