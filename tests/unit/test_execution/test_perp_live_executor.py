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

The P4-D review round adds its five fixes' guards: the wrapper's conditional read must
reach the fapi ALGO namespace (the fixture models ccxt's real positional signature, so
a params dict in the ``since`` slot fails loudly here), a rejected second leg unwinds
the LANDED stop and the decision-17 market-close still fires, the funding cursor is
born at session start (pre-session rows never book) and survives the restart, and the
recovered session's adopted group carries the pair's leg ids so the dead-man verifies
``confirmed`` on the first closed bar after a restart.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import ccxt
import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import OrderFillEvent
from hydra.core.types import MarketType, OrderType, Side, Symbol
from hydra.execution.exchange_client import ExchangeClient
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
from hydra.execution.session_recovery import TradingSession
from hydra.execution.session_wiring import SessionRuntime
from hydra.risk.market_rules import MarketRules
from hydra.strategy.config import StrategyRiskConfig
from tests.fixtures.binance_futures_trading import FakeBinanceUsdmTradingClient
from tests.unit.test_execution.session_harness import Harness, write_strategy

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

# the strategy risk block that arms a protective PAIR (stop + take-profit)
PAIR_RISK: dict[str, Any] = {
    "stop_loss_method": "fixed_pct",
    "stop_loss_value": 2.0,
    "take_profit_method": "fixed_pct",
    "take_profit_value": 3.0,
}


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

    async def test_a_rejected_second_leg_unwinds_the_landed_stop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A4 (the P4-D review): the pair places the STOP first, so a rejected
        TAKE-PROFIT must cancel the LANDED stop — the shipped test only covered a
        first-leg failure, where nothing had landed. A stray stop would fire at
        the old entry's stop price against a later position: an uncommanded
        leveraged close."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        real = fake.create_order

        async def reject_take_profit(
            symbol: str,
            side: str,
            order_type: str,
            quantity: Decimal,
            price: Decimal | None = None,
            stop_price: Decimal | None = None,
            params: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if (params or {}).get("takeProfitPrice") is not None:
                raise ccxt.InvalidOrder("binanceusdm {-1013} Filter failure: MIN_NOTIONAL")
            return await real(symbol, side, order_type, quantity, price, stop_price, params)

        monkeypatch.setattr(fake, "create_order", reject_take_profit)
        with pytest.raises(StopUnplaceableError, match="TAKE_PROFIT_MARKET leg was rejected"):
            await _place_pair(ex)
        monkeypatch.setattr(fake, "create_order", real)

        assert fake.open_algo("BTCUSDT") == []  # the landed STOP_MARKET was cancelled
        landed = [o for o in fake.algo_orders.values() if o.order_type == "STOP_MARKET"]
        assert landed and all(o.algo_status == "CANCELED" for o in landed)
        # the failed pair's row resolves through the poller's not-found window
        poller = _poller(ex)
        for _ in range(4):
            await poller.sweep()
        rows = await ex.live_orders.orders(ex._session_id, limit=10)
        pair_rows = [row for row in rows if row.order_type == "OCO"]
        assert len(pair_rows) == 1
        assert pair_rows[0].status.value == "rejected"

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
        fake.seed_income("BTCUSDT", "-0.75", ex._funding_cursor_ms + 1000, "314")

        await ex.collect_exchange_fills(None)

        funding = [f for f in ex.get_fills() if f.side == "FUNDING"]
        assert len(funding) == 1
        assert funding[0].net_pnl == D("-0.75")
        assert funding[0].order_id == "funding-314"
        assert pos.margin == margin_before - D("0.75")
        assert ex.get_account_snapshot().funding_applied == 1

        await ex.collect_exchange_fills(None)
        assert [f for f in ex.get_fills() if f.side == "FUNDING"] == funding

    async def test_pre_session_funding_rows_never_book(self) -> None:
        """A3 (the P4-D review): the funding cursor is born at session start — the
        shared one-way account's FUNDING_FEE rows settled before this sleeve
        existed (the user's own funding among them) never enter this sleeve's
        ledger; a cursor born at 0 booked them all as silent ledger corruption."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        fake.seed_income("BTCUSDT", "-40.00", 1786000001000, "900")
        fake.seed_income("BTCUSDT", "-35.50", 1786000002000, "901")
        ex = _executor(fake)

        await _open_long(ex, fake)  # the first sweep

        assert [f for f in ex.get_fills() if f.side == "FUNDING"] == []
        assert ex.get_account_snapshot().funding_applied == 0

    async def test_a_row_settled_after_the_session_start_still_books(self) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        await _open_long(ex, fake)
        fake.seed_income("BTCUSDT", "-0.75", ex._funding_cursor_ms + 1000, "902")

        await ex.collect_exchange_fills(None)

        funding = [f for f in ex.get_fills() if f.side == "FUNDING"]
        assert len(funding) == 1 and funding[0].net_pnl == D("-0.75")

    async def test_the_funding_cursor_survives_the_restart(self) -> None:
        """A3: a restored sleeve continues from its PERSISTED cursor — never
        re-seeded — so a row settled while the engine was down still books, and
        a pre-cursor row never does. A legacy zero cursor (a pre-fix state that
        swept nothing) keeps the session-start bound instead of booking history."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake, session="restart")
        await _open_long(ex, fake)
        settled_during = ex._funding_cursor_ms + 1000
        fake.seed_income("BTCUSDT", "-0.75", settled_during, "314")
        await ex.collect_exchange_fills(None)
        assert ex._funding_cursor_ms == settled_during

        ex2 = _executor(fake, session="restart")
        ex2._live_orders = ex._live_orders  # the DB rows survive the restart
        ex2._rebuild_from_state(ex.to_state())
        assert ex2._funding_cursor_ms == settled_during  # persisted, not re-seeded

        fake.seed_income("BTCUSDT", "-0.30", settled_during + 2000, "315")  # while down
        fake.seed_income("BTCUSDT", "-9.00", settled_during - 500, "316")  # pre-cursor
        await ex2.collect_exchange_fills(None)
        assert [f.net_pnl for f in ex2.get_fills() if f.side == "FUNDING"] == [D("-0.30")]

        ex3 = _executor(fake, session="restart")
        ex3._rebuild_from_state({**ex.to_state(), "funding_cursor_ms": 0})
        assert ex3._funding_cursor_ms > 0  # a legacy zero cursor never re-opens history

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

        fake.seed_income("BTCUSDT", "-0.75", ex._funding_cursor_ms + 2000, "315")
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
        fake.seed_income("BTCUSDT", "-50", ex._funding_cursor_ms + 2000, "316")

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

    async def test_the_wrappers_conditional_read_hits_the_algo_namespace(self) -> None:
        """A1 (the P4-D review), pinned one seam deeper: the fixture models ccxt
        4.5.88's real positional signature ``(symbol, since, limit, params)``, so
        the ExchangeClient wrapper's conditional read must land the params dict
        in slot 4 — positionally in ``since`` it silently reads the REGULAR
        namespace and every armed ladder answers ``missing``."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        await fake.create_order(
            "BTCUSDT",
            "SELL",
            "market",
            D("0.01"),
            None,
            None,
            {"newClientOrderId": "hyd-stop", "reduceOnly": True, "stopLossPrice": "95000"},
        )
        await fake.create_order(
            "BTCUSDT",
            "SELL",
            "LIMIT",
            D("0.01"),
            D("90000"),
            None,
            {"newClientOrderId": "hyd-limit"},
        )
        client = ExchangeClient("binanceusdm", {}, market_type=MarketType.FUTURES)
        client._exchange = fake  # the fixture stands in for the real ccxt instance

        conditional = await client.fetch_open_orders("BTCUSDT", conditional=True)
        assert [o["clientOrderId"] for o in conditional] == ["hyd-stop"]
        assert [o["triggerPrice"] for o in conditional] == ["95000"]
        regular = await client.fetch_open_orders("BTCUSDT")
        assert [o["clientOrderId"] for o in regular] == ["hyd-limit"]


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

    async def test_the_adopted_group_carries_the_legs_and_verifies(self) -> None:
        """A2 (the P4-D review): the recovery adopt must build the group from the
        tracked pair row's leg client ids — ``verify_perp_ladder`` matches wire
        legs by them. A hydra-local group with ``leg_ids=()`` answered ``missing``
        with both legs correctly resting, paging the dead-man forever."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        ex = _executor(fake)
        protect = self._protect(ex, self.PAIR_POLICY)
        await _open_long(ex, fake)
        await protect.on_fill(_entry_fill())
        running = protect.armed("BTCUSDT")
        assert running is not None
        assert (await verify_perp_ladder(fake, "BTCUSDT", running)).state == "confirmed"

        protect2 = self._protect(ex, self.PAIR_POLICY)  # a restart: a fresh manager adopts
        await protect2.adopt(["BTCUSDT"])

        adopted = protect2.armed("BTCUSDT")
        assert adopted is not None
        assert adopted.order_id == running.order_id
        assert adopted.order_type == OrderType.OCO
        resting = fake.open_algo("BTCUSDT")
        assert sorted(o.client_algo_id for o in resting) == sorted(adopted.leg_ids)
        assert (await verify_perp_ladder(fake, "BTCUSDT", adopted)).state == "confirmed"


def _runtime_executor(
    fake: FakeBinanceUsdmTradingClient, session_id: str
) -> LivePerpTradingExecutor:
    fake.leverage["BTCUSDT"] = 2
    ex = LivePerpTradingExecutor(
        fake,
        exchange_id="binanceusdm",
        initial_balances={"USDT": D("10000")},
        db_pool=None,
        strategy_id="strat",
        session_id=session_id,
        market_rules={"BTCUSDT": RULES},
        leverage=2,
        poll_interval=0.05,
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", D("100000"))
    return ex


async def _perp_runtime(
    h: Harness,
    strategies_dir: Path,
    fake: FakeBinanceUsdmTradingClient,
    session_id: str,
    *,
    executor: LivePerpTradingExecutor | None = None,
    risk: dict[str, Any] | None = None,
) -> tuple[SessionRuntime, LivePerpTradingExecutor, list[str]]:
    """A live perp SessionRuntime over the harness's real manager (no DB)."""
    sid = write_strategy(
        strategies_dir,
        f"perp-rt-{session_id}",
        exchange_id="binanceusdm",
        market_type="FUTURES",
        leverage=2,
        risk=risk,
    )
    assert h.mgr is not None and h.mgr.risk is not None
    cfg = h.mgr._find_strategy_config(sid)
    session = TradingSession(
        session_id=session_id,
        strategy_id=sid,
        trading_mode="live",
        exchange_id="binanceusdm",
        symbols=["BTCUSDT"],
        timeframe="1h",
        paper_capital=D("10000"),
    )
    session.status = "running"
    ex = executor if executor is not None else _runtime_executor(fake, session_id)
    failures: list[str] = []

    async def on_failure(_session: TradingSession, reason: str) -> None:
        failures.append(reason)

    runtime = SessionRuntime(
        session,
        cfg,
        executor=ex,
        risk=h.mgr.risk,
        clients=h.clients(),
        hydra_config=HydraConfig(),
        db_pool=None,
        required_history=2,
        on_failure=on_failure,
    )
    await h.mgr.risk.register_session(
        session_id,
        ex,
        allocated=D("10000"),
        symbols=["BTCUSDT"],
        exchange_id="binanceusdm",
        strategy_id=sid,
        leverage=2,
    )
    await h.mgr.risk.on_mark(
        "binanceusdm", "BTCUSDT", D("100000"), datetime(2026, 10, 27, tzinfo=UTC)
    )
    return runtime, ex, failures


class TestRecoveredDeadMan:
    """The dead-man after a restart: the adopted group must verify ``confirmed``
    on the first closed bar, and a rejected second leg must still end in the
    decision-17 market-close (A2/A4 of the P4-D review), at the wiring seam."""

    @pytest.fixture(autouse=True)
    def _fresh_gauge(self) -> Iterator[None]:
        metrics._reset_metrics()
        yield
        metrics._reset_metrics()

    async def test_a_restarted_session_with_a_resting_pair_verifies_confirmed(
        self, strategies_dir: Path
    ) -> None:
        h = Harness(strategies_dir)
        await h.start()
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        runtime2: SessionRuntime | None = None
        try:
            runtime, ex, _ = await _perp_runtime(
                h, strategies_dir, fake, "perp-rec", risk=PAIR_RISK
            )
            await runtime.build()
            await runtime.wire()
            await _open_long(ex, fake)
            await runtime.protect.on_fill(_entry_fill())
            running = runtime.protect.armed("BTCUSDT")
            assert running is not None
            assert (await runtime._live_coverage())["BTCUSDT"].state == "confirmed"

            # the restart: a fresh executor over the same exchange, rows and state
            ex2 = _runtime_executor(fake, "perp-rec")
            ex2._live_orders = ex._live_orders
            ex2._rebuild_from_state(ex.to_state())
            runtime2, _, _ = await _perp_runtime(
                h, strategies_dir, fake, "perp-rec", executor=ex2, risk=PAIR_RISK
            )
            await runtime2.build()
            await runtime2.protect.adopt(["BTCUSDT"])  # what _launch(recovering=True) does
            assert runtime2.protect.armed("BTCUSDT") is not None

            await runtime2._check_live_coverage()  # the first closed bar's dead-man hook
            gap = REGISTRY.get_sample_value(
                "hydra_live_protective_gap",
                {
                    "session_id": "perp-rec",
                    "strategy_id": runtime2.cfg.id,
                    "exchange_id": "binanceusdm",
                    "symbol": "BTCUSDT",
                },
            )
            assert gap is None  # confirmed: the gap never sets — no page, no storm
            assert (await runtime2._live_coverage())["BTCUSDT"].state == "confirmed"
        finally:
            if runtime2 is not None:
                await runtime2.shutdown()
            await runtime.shutdown()
            await h.close()

    async def test_a_rejected_second_leg_still_fires_the_decision_17_close(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        h = Harness(strategies_dir)
        await h.start()
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        try:
            runtime, ex, failures = await _perp_runtime(
                h, strategies_dir, fake, "perp-d17", risk=PAIR_RISK
            )
            await runtime.build()
            await runtime.wire()
            await _open_long(ex, fake)
            real = fake.create_order

            async def reject_take_profit(
                symbol: str,
                side: str,
                order_type: str,
                quantity: Decimal,
                price: Decimal | None = None,
                stop_price: Decimal | None = None,
                params: dict[str, Any] | None = None,
            ) -> dict[str, Any]:
                if (params or {}).get("takeProfitPrice") is not None:
                    raise ccxt.InvalidOrder("binanceusdm {-1013} Filter failure: MIN_NOTIONAL")
                return await real(symbol, side, order_type, quantity, price, stop_price, params)

            monkeypatch.setattr(fake, "create_order", reject_take_profit)
            await runtime._on_fill(_entry_fill())  # the arm attempt fails inside
            monkeypatch.setattr(fake, "create_order", real)

            assert runtime.protect.armed("BTCUSDT") is None
            assert runtime.protect.failures
            assert "TAKE_PROFIT_MARKET leg was rejected" in runtime.protect.failures[-1]
            # the decision-17 close fired on the wire: a reduce-only MARKET SELL
            closes = [
                c
                for c in fake.create_calls
                if c["side"] == "SELL" and c["type"] == "MARKET" and c["params"].get("reduceOnly")
            ]
            assert closes
            assert fake.open_algo("BTCUSDT") == []  # nothing rests at the old stop price

            await runtime._poller.sweep()  # the close fill books
            assert not [p for p in await ex.get_positions("BTCUSDT") if p.quantity > 0]
            assert failures == []
        finally:
            await runtime.shutdown()
            await h.close()


def _entry_fill() -> OrderFillEvent:
    return OrderFillEvent(
        order_id="entry-1",
        symbol=Symbol("BTCUSDT"),
        side=Side.BUY,
        quantity=D("0.02"),
        price=D("100000"),
        tag="entry",
    )
