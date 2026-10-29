"""The live perp executor against the fake fapi account: the ladder, the sibling-cancel,
funding income, and the deficit-only reconcile — real numbers at the ccxt seam.

The demanded cases, pinned exactly: both ladder legs are reduce-only on the wire, the
trail variant carries ``callbackRate`` PERCENT (a red test asserts ``trailingDelta``
NEVER appears on the futures wire), the sibling-cancel fires on BOTH paths (the
fill-driven one and the reconcile-driven one — a leg that filled while the sweeps were
silent is caught by the reconcile's own sweep), the funding income books so the wallet
does not read as drift, and the reconcile pages ONLY on the deficit direction (a surplus
exchange position — the user's own on the shared account — is not drift; the sleeve is
never rebased from the compare).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import OrderFillEvent
from hydra.core.types import MarketType, Side, Symbol
from hydra.execution.exchange_stops import StopUnplaceableError
from hydra.execution.order_manager import OrderManager
from hydra.execution.paper_trading import OrderRejectedError
from hydra.execution.perp_live import (
    LivePerpOrderPoller,
    LivePerpTradingExecutor,
    reconcile_perp_session,
    verify_perp_ladder,
)
from hydra.execution.protective_orders import ProtectiveOrderManager
from hydra.risk.market_rules import MarketRules
from hydra.strategy.config import StrategyRiskConfig
from tests.fixtures.binance_futures_trading import FakeBinanceUsdmTradingClient

D = Decimal

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
    trailing_delta_bips=None,
    trailing_callback_rate_pct=(D("0.1"), D("10")),
)


def _executor(
    fake: FakeBinanceUsdmTradingClient,
    *,
    leverage: int = 2,
    initial: str = "10000",
    session: str = "sess",
) -> LivePerpTradingExecutor:
    fake.leverage["BTCUSDT"] = leverage  # the account's own margin model matches
    ex = LivePerpTradingExecutor(
        fake,
        exchange_id="binanceusdm",
        initial_balances={"USDT": D(initial)},
        db_pool=None,
        strategy_id="strat",
        session_id=session,
        market_rules={"BTCUSDT": RULES},
        leverage=leverage,
        poll_interval=5.0,
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", D("100000"))
    return ex


def _poller(ex: LivePerpTradingExecutor) -> LivePerpOrderPoller:
    om = OrderManager(ex, InMemoryEventBus(), session_id=ex._session_id)
    return LivePerpOrderPoller(
        ex, om, interval=0.05, resolve_grace=ex.resolve_grace, not_found_window=0.0
    )


async def _open_long(
    ex: LivePerpTradingExecutor, fake: FakeBinanceUsdmTradingClient, qty: str = "0.02"
) -> None:
    """A filled real entry: MARKET BUY on the wire, booked through the sweep."""
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D(qty), params={"tag": "entry"})
    await ex.collect_exchange_fills(None)
    assert any(p.quantity > 0 for p in await ex.get_positions("BTCUSDT"))


async def _place_pair(ex: LivePerpTradingExecutor, qty: str = "0.02") -> dict:
    return await ex.create_order(
        "BTCUSDT",
        "SELL",
        "OCO",
        D(qty),
        price=D("105000"),
        stop_price=D("95000"),
        params={"tag": "protective", "reduce_only": True},
    )


class TestLadderPlacement:
    async def test_the_pair_places_two_reduce_only_conditional_legs(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)

        resp = await _place_pair(ex)

        assert resp["type"] == "OCO"
        assert resp["stop_loss_order_id"] != resp["take_profit_order_id"]
        tp_calls = [c for c in fake.create_calls if c["params"].get("takeProfitPrice")]
        sl_calls = [c for c in fake.create_calls if c["params"].get("stopLossPrice")]
        assert len(tp_calls) == 1 and len(sl_calls) == 1
        assert tp_calls[0]["params"]["takeProfitPrice"] == "105000.00"  # tick-rounded
        assert sl_calls[0]["params"]["stopLossPrice"] == "95000.00"
        for call in (*tp_calls, *sl_calls):
            assert call["params"]["reduceOnly"] is True
            assert call["side"] == "SELL"
            assert call["params"]["newClientOrderId"].startswith("hyd-")
        tp = fake.algo_by_client_id(resp["take_profit_order_id"])
        sl = fake.algo_by_client_id(resp["stop_loss_order_id"])
        assert tp is not None and tp.order_type == "TAKE_PROFIT_MARKET"
        assert sl is not None and sl.order_type == "STOP_MARKET"
        assert [leg["client_order_id"] for leg in resp["legs"]] == [
            resp["stop_loss_order_id"],
            resp["take_profit_order_id"],
        ]
        row = await ex.live_orders.by_client_order_id(resp["id"])
        assert row is not None
        assert row.order_type == "OCO"
        assert row.leg_client_order_ids == (
            resp["take_profit_order_id"],
            resp["stop_loss_order_id"],
        )
        assert row.reduce_only is True

    async def test_a_short_ladder_buys_back_with_inverted_geometry(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await ex.create_order("BTCUSDT", "SELL", "MARKET", D("0.02"), params={"tag": "entry"})
        await ex.collect_exchange_fills(None)
        positions = await ex.get_positions("BTCUSDT")
        assert positions and positions[0].direction.value == "SHORT"

        resp = await ex.create_order(
            "BTCUSDT",
            "BUY",
            "OCO",
            D("0.02"),
            price=D("95000"),
            stop_price=D("105000"),
            params={"tag": "protective", "reduce_only": True},
        )
        tp = fake.algo_by_client_id(resp["take_profit_order_id"])
        sl = fake.algo_by_client_id(resp["stop_loss_order_id"])
        assert tp is not None and tp.trigger_price == D("95000")
        assert sl is not None and sl.trigger_price == D("105000")
        for call in fake.create_calls:
            if call["params"].get("takeProfitPrice") or call["params"].get("stopLossPrice"):
                assert call["side"] == "BUY"

    async def test_the_trail_variant_carries_callback_rate_percent(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)

        resp = await ex.create_order(
            "BTCUSDT",
            "SELL",
            "TRAILING_STOP",
            D("0.02"),
            params={"tag": "protective", "reduce_only": True, "trail_pct": "0.025"},
        )
        trail_calls = [c for c in fake.create_calls if "callbackRate" in c["params"]]
        assert len(trail_calls) == 1
        assert trail_calls[0]["params"]["callbackRate"] == "2.5"
        assert trail_calls[0]["params"]["reduceOnly"] is True
        algo = fake.algo_by_client_id(resp["client_order_id"])
        assert algo is not None
        assert algo.order_type == "TRAILING_STOP_MARKET"
        assert algo.callback_rate == D("2.5")

    async def test_trailing_delta_never_touches_the_futures_wire(self) -> None:
        """The red test: spot's TRAILING_DELTA bips NEVER appear in a fapi order,
        whatever the flow placed — pair, single leg or trail."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        await _place_pair(ex)
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            "STOP_MARKET",
            D("0.02"),
            stop_price=D("95000"),
            params={"tag": "protective", "reduce_only": True},
        )
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            "TRAILING_STOP",
            D("0.02"),
            params={"tag": "protective", "reduce_only": True, "trail_pct": "0.01"},
        )
        assert len(fake.create_calls) >= 4
        for call in fake.create_calls:
            assert "trailingDelta" not in call["params"], call

    async def test_an_out_of_bounds_trail_is_refused(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        with pytest.raises(OrderRejectedError, match="callbackRate bounds"):
            await ex.create_order(
                "BTCUSDT",
                "SELL",
                "TRAILING_STOP",
                D("0.02"),
                params={"tag": "protective", "reduce_only": True, "trail_pct": "0.15"},
            )

    async def test_a_rejected_leg_unwinds_the_pair_and_raises(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        fake.fail_create = "invalid_order"
        with pytest.raises(StopUnplaceableError):
            await _place_pair(ex)
        assert fake.open_algo("BTCUSDT") == []

    async def test_a_take_profit_past_the_mark_drops_the_leg(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        resp = await ex.create_order(
            "BTCUSDT",
            "SELL",
            "OCO",
            D("0.02"),
            price=D("99999"),
            stop_price=D("95000"),
            params={"tag": "protective", "reduce_only": True},
        )
        assert resp["price"] is None
        resting = fake.open_algo("BTCUSDT")
        assert len(resting) == 1 and resting[0].order_type == "STOP_MARKET"

    async def test_cancelling_the_pair_cancels_both_legs(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        resp = await _place_pair(ex)

        result = await ex.cancel_order(resp["id"], "BTCUSDT")

        assert result["status"] == "CANCELLED"
        assert fake.open_algo("BTCUSDT") == []
        row = await ex.live_orders.by_client_order_id(resp["id"])
        assert row is not None and row.status.value == "canceled"


class TestSiblingCancel:
    async def test_the_fill_driven_path_cancels_the_sibling(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        resp = await _place_pair(ex)
        resting = fake.open_algo("BTCUSDT")
        fired, sibling = resting[0].client_algo_id, resting[1].client_algo_id

        fake.trigger(fired, "105000")
        fills = await ex.collect_exchange_fills(None)

        assert len(fills) == 1 and fills[0].side == "SELL"
        assert fills[0].order_id == resp["id"]
        cancelled = fake.algo_by_client_id(sibling)
        assert cancelled is not None and cancelled.algo_status == "CANCELED"
        row = await ex.live_orders.by_order_list_id(resp["id"], ex._session_id)
        assert row is not None and row.status.value in ("filled", "canceled")
        assert not await ex.get_positions("BTCUSDT")

    async def test_the_reconcile_driven_path_cancels_the_sibling(self) -> None:
        """A leg filled while no sweep ran: ONE reconcile pass still cancels the sibling."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        resp = await _place_pair(ex)
        poller = _poller(ex)
        resting = fake.open_algo("BTCUSDT")
        fired, sibling = resting[0].client_algo_id, resting[1].client_algo_id

        fake.trigger(fired, "105000")
        still = fake.algo_by_client_id(sibling)
        assert still is not None and still.algo_status == "NEW"

        status = await reconcile_perp_session(
            ex.live_orders, fake, ex, poller=poller, previous=None
        )

        assert status.state == "ok", status.detail
        cancelled = fake.algo_by_client_id(sibling)
        assert cancelled is not None and cancelled.algo_status == "CANCELED"
        row = await ex.live_orders.by_order_list_id(resp["id"], ex._session_id)
        assert row is not None and row.status.value in ("filled", "canceled")
        assert not await ex.get_positions("BTCUSDT")


class TestFundingIncome:
    async def test_funding_books_from_the_income_rows_once(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        pos = ex._positions["BTCUSDT"]
        margin_before = pos.margin
        fake.seed_income("BTCUSDT", "-0.75", 1786000001000, "314")

        await ex.collect_exchange_fills(None)

        funding = [f for f in ex.get_fills() if f.side == "FUNDING"]
        assert len(funding) == 1
        assert funding[0].net_pnl == D("-0.75")
        assert funding[0].order_id == "funding-314"
        assert pos.margin == margin_before - D("0.75")
        assert ex.get_account_snapshot().funding_applied == 1

        await ex.collect_exchange_fills(None)
        assert [f for f in ex.get_fills() if f.side == "FUNDING"] == funding

    async def test_a_late_row_after_the_close_books_to_the_wallet(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            "MARKET",
            D("0.02"),
            params={"tag": "exit", "reduce_only": True},
        )
        await ex.collect_exchange_fills(None)
        assert not ex._positions["BTCUSDT"].qty
        wallet_after_close = ex._wallet

        fake.seed_income("BTCUSDT", "-0.75", 1786000002000, "315")
        await ex.collect_exchange_fills(None)

        assert ex._wallet == wallet_after_close - D("0.75")

    async def test_booked_funding_keeps_the_wallet_compare_clean(self) -> None:
        """The debit the exchange took must not read as the sleeve's missing money:
        a 50 USDT funding row is far above the 0.1 % drift threshold, and booking
        it keeps the reconcile ok — unbooked it would page a wallet deficit."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        poller = _poller(ex)
        await _open_long(ex, fake)
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            "MARKET",
            D("0.02"),
            params={"tag": "exit", "reduce_only": True},
        )
        await ex.collect_exchange_fills(None)
        fake.seed_income("BTCUSDT", "-50", 1786000002000, "316")

        await ex.collect_exchange_fills(None)  # books the row into the sleeve

        status = await reconcile_perp_session(
            ex.live_orders, fake, ex, poller=poller, previous=None
        )
        assert status.state == "ok", status.detail


class TestPerpReconcile:
    async def test_a_clean_pass_is_ok(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        poller = _poller(ex)
        await _open_long(ex, fake)
        status = await reconcile_perp_session(
            ex.live_orders, fake, ex, poller=poller, previous=None
        )
        assert status.state == "ok", status.detail

    async def test_a_surplus_exchange_position_is_not_drift(self) -> None:
        """The false-drift scenario: the user's own long on the shared one-way
        account does nothing — only the sleeve's unbacked claim pages."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        poller = _poller(ex)
        await _open_long(ex, fake)
        fake.positions["BTCUSDT"].qty += D("0.05")

        status = await reconcile_perp_session(
            ex.live_orders, fake, ex, poller=poller, previous=None
        )

        assert status.state == "ok", status.detail

    async def test_a_position_deficit_pages_and_never_rebases_the_sleeve(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        poller = _poller(ex)
        await _open_long(ex, fake)
        sleeve_before = ex._positions["BTCUSDT"].qty
        fake.positions["BTCUSDT"].qty = D("0")  # the exchange liquidated it

        status = await reconcile_perp_session(
            ex.live_orders, fake, ex, poller=poller, previous=None
        )

        assert status.state == "mismatch"
        assert "position deficit BTCUSDT" in status.detail
        assert "(not backed by positionRisk)" in status.detail
        assert ex._positions["BTCUSDT"].qty == sleeve_before

    async def test_a_wallet_deficit_pages(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        poller = _poller(ex)
        await _open_long(ex, fake)
        fake.wallet -= D("3000")  # money gone from the exchange

        status = await reconcile_perp_session(
            ex.live_orders, fake, ex, poller=poller, previous=None
        )
        assert status.state == "mismatch"
        assert "balance drift USDT" in status.detail


class TestCoverageVerify:
    async def test_a_resting_pair_confirms_over_both_legs(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        resp = await _place_pair(ex)
        expected = SimpleNamespace(
            order_id=str(resp["id"]),
            leg_ids=(resp["stop_loss_order_id"], resp["take_profit_order_id"]),
        )

        state = await verify_perp_ladder(fake, "BTCUSDT", expected)
        assert state.state == "confirmed"
        assert state.kind == "oco"
        assert state.stop_price == 95000.0
        assert state.take_profit == 105000.0

        fake.trigger(fake.open_algo("BTCUSDT")[0].client_algo_id, "105000")
        state = await verify_perp_ladder(fake, "BTCUSDT", expected)
        assert state.state == "missing"

    async def test_without_an_expectation_any_owned_reduce_only_leg_counts(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        await ex.create_order(
            "BTCUSDT",
            "SELL",
            "STOP_MARKET",
            D("0.02"),
            stop_price=D("95000"),
            params={"tag": "protective", "reduce_only": True},
        )
        state = await verify_perp_ladder(fake, "BTCUSDT", None)
        assert state.state == "confirmed"
        assert state.stop_price == 95000.0

    async def test_the_users_own_ladder_does_not_count_without_an_expectation(self) -> None:
        """R5: only hydra's ``hyd-`` client ids are adopted by the no-expectation read."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        fake._next_algo_id += 1
        await fake.create_order(
            "BTCUSDT",
            "SELL",
            "market",
            D("0.01"),
            None,
            None,
            {"newClientOrderId": "user-owned", "reduceOnly": True, "stopLossPrice": "90000"},
        )
        state = await verify_perp_ladder(fake, "BTCUSDT", None)
        assert state.state == "missing"

    async def test_an_unreadable_exchange_is_unknown(self) -> None:
        class _Broken:
            async def fetch_open_orders(
                self, symbol: str, *, conditional: bool = False
            ) -> list[dict]:
                raise RuntimeError("fapi down")

        state = await verify_perp_ladder(_Broken(), "BTCUSDT", None)
        assert state.state == "unknown"
        assert state.error == "fapi down"


class TestKillSwitchSurface:
    async def test_cancel_all_sweeps_both_namespaces(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        await _place_pair(ex)
        await ex.create_order(
            "BTCUSDT", "SELL", "LIMIT", D("0.01"), price=D("90000"), params={"tag": "manual"}
        )

        await ex.cancel_all_orders("BTCUSDT")

        assert fake.cancel_all_calls == ["BTCUSDT"]
        assert fake.open_algo("BTCUSDT") == []
        assert fake.open_plain("BTCUSDT") == []


class TestProtectiveDiscipline:
    """The P3-B re-arm/replace machinery riding the NO-OCO ladder: the protective
    manager's arm/disarm/re-arm go through ``create_order``/``cancel_order``, so the
    pair arms under the same live discipline as spot — a confirmed group carrying
    BOTH leg ids, both legs cancelled on disarm, the group restored on re-arm."""

    PAIR_POLICY = StrategyRiskConfig(
        stop_loss_method="fixed_pct",
        stop_loss_value=2.0,
        take_profit_method="fixed_pct",
        take_profit_value=3.0,
    )
    STOP_POLICY = StrategyRiskConfig(stop_loss_method="fixed_pct", stop_loss_value=2.0)

    def _protect(
        self, ex: LivePerpTradingExecutor, policy: StrategyRiskConfig
    ) -> ProtectiveOrderManager:
        om = OrderManager(ex, InMemoryEventBus(), session_id=ex._session_id)
        return ProtectiveOrderManager(
            om,
            ex,
            strategy_id="strat",
            exchange_id="binanceusdm",
            market_type=MarketType.FUTURES,
            policy=policy,
            atr=lambda symbol: None,
        )

    async def test_an_entry_fill_arms_a_confirmed_pair_group(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        protect = self._protect(ex, self.PAIR_POLICY)
        await _open_long(ex, fake)

        await protect.on_fill(_entry_fill())

        group = protect.armed("BTCUSDT")
        assert group is not None
        assert group.confirmed is True
        assert len(group.leg_ids) == 2
        assert group.spec.stop_loss == D("98000")  # 2% below the 100000 entry
        assert group.spec.take_profit == D("103000")  # 3% above
        resting = fake.open_algo("BTCUSDT")
        assert sorted(str(o.algo_id) for o in resting) == sorted(group.leg_ids)
        assert {o.order_type for o in resting} == {"STOP_MARKET", "TAKE_PROFIT_MARKET"}
        assert all(o.reduce_only for o in resting)

        await protect.disarm("BTCUSDT")
        assert protect.armed("BTCUSDT") is None
        assert fake.open_algo("BTCUSDT") == []

        await protect.rearm("BTCUSDT")
        group = protect.armed("BTCUSDT")
        assert group is not None and group.confirmed is True
        assert len(fake.open_algo("BTCUSDT")) == 2

    async def test_a_stop_only_policy_arms_a_confirmed_single_leg(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        protect = self._protect(ex, self.STOP_POLICY)
        await _open_long(ex, fake)

        await protect.on_fill(_entry_fill())

        group = protect.armed("BTCUSDT")
        assert group is not None
        assert group.confirmed is True  # a single conditional leg carries itself
        assert len(group.leg_ids) == 1
        resting = fake.open_algo("BTCUSDT")
        assert len(resting) == 1 and resting[0].order_type == "STOP_MARKET"
        assert str(resting[0].algo_id) == group.leg_ids[0]

        await protect.clear()
        assert protect.armed("BTCUSDT") is None
        assert fake.open_algo("BTCUSDT") == []


def _entry_fill() -> OrderFillEvent:
    return OrderFillEvent(
        order_id="entry-1",
        symbol=Symbol("BTCUSDT"),
        side=Side.BUY,
        quantity=D("0.02"),
        price=D("100000"),
        tag="entry",
    )
