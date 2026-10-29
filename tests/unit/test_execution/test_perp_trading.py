"""The perp paper executor's margin math, funding accrual, liquidation and shorts.

Real numbers at every seam: entries move ``notional / leverage`` of margin, round trips
net both fees, funding debits exactly ``qty x rate x mark`` (longs pay positive rates),
liquidation closes at the equity-equals-maintenance price and loses the margin, and a
window with no stored rows accrues nothing while counting the skip.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from hydra.core.types import OHLCV, Direction
from hydra.execution.paper_trading import InsufficientBalanceError, PaperSessionHaltedError
from hydra.execution.perp_trading import (
    MAINTENANCE_MARGIN_RATE,
    PerpPaperTradingExecutor,
    static_funding_reader,
)
from tests.unit.test_execution.test_paper_persistence import RecordingPool

D = Decimal
ZERO = D("0")
H = timedelta(hours=1)


def make(
    *,
    capital: str = "1000",
    leverage: int = 2,
    mmr: str | None = None,
    taker: str = "0.0004",
    funding: dict[str, list[tuple[datetime, Decimal]]] | None = None,
) -> PerpPaperTradingExecutor:
    kwargs: dict[str, Any] = {}
    if mmr is not None:
        kwargs["maintenance_margin_rate"] = D(mmr)
    return PerpPaperTradingExecutor(
        initial_balances={"USDT": D(capital)},
        slippage_pct=ZERO,
        maker_fee=D(taker),
        taker_fee=D(taker),
        leverage=leverage,
        funding_reader=static_funding_reader(funding or {}),
        **kwargs,
    )


async def market_buy(ex: PerpPaperTradingExecutor, qty: str, price: str) -> dict[str, Any]:
    ex.set_mark("BTCUSDT", D(price))
    return await ex.create_order("BTCUSDT", "BUY", "MARKET", D(qty))


async def market_sell(ex: PerpPaperTradingExecutor, qty: str, price: str) -> dict[str, Any]:
    ex.set_mark("BTCUSDT", D(price))
    return await ex.create_order("BTCUSDT", "SELL", "MARKET", D(qty))


def pos(ex: PerpPaperTradingExecutor) -> Any:
    return ex._positions["BTCUSDT"]


def bar(ts: datetime, o: str, h: str, low: str, c: str) -> OHLCV:
    return OHLCV(D(o), D(h), D(low), D(c), D("10"), ts)


class TestMarginAccounting:
    async def test_entry_moves_margin_and_fees_out_of_the_wallet(self) -> None:
        ex = make(leverage=2)
        await market_buy(ex, "1", "100")
        snap = ex.get_account_snapshot()
        # margin 100/2 = 50, fee 100 x 0.0004 = 0.04
        assert (snap.wallet_available, snap.margin_used, snap.equity) == (
            D("949.96"),
            D("50"),
            D("999.96"),
        )
        assert (pos(ex).qty, pos(ex).entry_price, pos(ex).margin) == (D("1"), D("100"), D("50"))

    async def test_insufficient_margin_is_refused(self) -> None:
        ex = make(capital="10", leverage=2)
        with pytest.raises(InsufficientBalanceError):
            await market_buy(ex, "1", "100")

    async def test_leverage_scales_the_margin_not_the_position(self) -> None:
        ex = make(leverage=3)
        await market_buy(ex, "1", "100")
        assert ex.get_account_snapshot().margin_used == D("100") / D("3")
        # fetch_balance is the free wallet; get_balance is cash net of the borrowed
        # notional: capital - full notional - fees (the account read adds qty x mark)
        assert (await ex.fetch_balance())["USDT"] == D("1000") - D("100") / D("3") - D("0.04")
        assert (await ex.get_balance())["USDT"] == D("1000") - D("100") - D("0.04")


class TestRoundTrips:
    async def test_long_round_trip_nets_both_fees(self) -> None:
        ex = make(leverage=2)
        await market_buy(ex, "1", "100")
        fill = await market_sell(ex, "1", "110")
        # gross 10, exit fee 110 x 0.0004 = 0.044, pro-rata entry fee 0.04
        assert fill["pnl"] == 10.0
        assert D(str(fill["net_pnl"])) == D("10") - D("0.044") - D("0.04")
        snap = ex.get_account_snapshot()
        assert snap.equity == D("1000") + D("9.916")
        assert snap.realized_net == D("9.916")
        assert snap.base_totals == {"BTC": ZERO}
        assert (await ex.get_positions()) == []

    async def test_short_round_trip_nets_both_fees(self) -> None:
        ex = make(leverage=2)
        entry = await market_sell(ex, "1", "100")
        assert entry["net_pnl"] is None  # a SELL that opens a short realizes nothing
        [position] = await ex.get_positions()
        assert (position.direction, position.quantity, position.avg_entry_price) == (
            Direction.SHORT,
            D("1"),
            D("100"),
        )
        fill = await market_buy(ex, "1", "90")
        # short gross = entry - exit = 10, exit fee 90 x 0.0004 = 0.036, entry fee 0.04
        assert fill["pnl"] == 10.0
        assert D(str(fill["net_pnl"])) == D("10") - D("0.036") - D("0.04")
        assert ex.get_account_snapshot().equity == D("1000") + D("9.924")

    async def test_partial_exit_releases_pro_rata_margin(self) -> None:
        ex = make(leverage=2)
        await market_buy(ex, "2", "100")
        fill = await market_sell(ex, "1", "100")
        # flat exit: no price PnL, half the margin returns; the exit fee 0.04 and the
        # pro-rata entry fee 0.04 net against it
        assert D(str(fill["net_pnl"])) == -D("0.04") - D("0.04")
        assert fill["fee"]["cost"] == 0.04
        snap = ex.get_account_snapshot()
        assert snap.margin_used == D("50")
        assert pos(ex).qty == D("1")

    async def test_flatten_closes_a_short_by_buy_back(self) -> None:
        ex = make(leverage=2)
        await market_sell(ex, "1", "100")
        ex.set_mark("BTCUSDT", D("95"))
        fills = await ex.flatten(reason="kill")
        assert [f.side for f in fills] == ["BUY"]
        assert fills[0].reason == "kill"
        assert (await ex.get_positions()) == []


class TestFunding:
    async def test_long_pays_a_positive_rate_at_the_mark_carried_in(self) -> None:
        settled = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        ex = make(leverage=2, funding={"BTCUSDT": [(settled, D("0.0001"))]})
        await market_buy(ex, "1", "100")
        # the first closed bar anchors the window; the next one carries the settlement
        await ex.on_closed_bar("BTCUSDT", bar(settled - H, "100", "100", "100", "100"))
        await ex.on_closed_bar("BTCUSDT", bar(settled + H, "100", "100", "100", "104"))
        # payment = 1 x 0.0001 x 100 (the mark carried into the bar, not its close)
        assert pos(ex).margin == D("50") - D("0.01")
        snap = ex.get_account_snapshot()
        assert snap.funding_applied == 1
        assert snap.funding_missing == 0
        [row] = [f for f in ex.get_fills() if f.side == "FUNDING"]
        assert (row.net_pnl, row.price, row.timestamp) == (-D("0.01"), D("100"), settled)

    async def test_short_receives_a_positive_rate(self) -> None:
        settled = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        ex = make(leverage=2, funding={"BTCUSDT": [(settled, D("0.0001"))]})
        await market_sell(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(settled - H, "100", "100", "100", "100"))
        await ex.on_closed_bar("BTCUSDT", bar(settled + H, "100", "100", "100", "104"))
        assert pos(ex).margin == D("50") + D("0.01")
        assert ex.get_account_snapshot().funding_applied == 1

    async def test_no_rows_accrues_nothing_and_counts_the_missing_settlement(self) -> None:
        # 07:00 -> 08:00 crosses the 08:00 settlement boundary; the table has no row
        start = datetime(2026, 1, 5, 7, 0, tzinfo=UTC)
        ex = make(leverage=2, funding={})
        await market_buy(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(start - H, "100", "100", "100", "100"))
        await ex.on_closed_bar("BTCUSDT", bar(start + H, "100", "100", "100", "100"))
        snap = ex.get_account_snapshot()
        assert snap.funding_applied == 0
        assert snap.funding_missing == 1
        assert pos(ex).margin == D("50")
        assert not [f for f in ex.get_fills() if f.side == "FUNDING"]

    async def test_a_window_without_a_settlement_hour_is_not_a_missing_row(self) -> None:
        # 12:00 -> 13:00 touches no hour Binance settles on any symbol
        start = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)
        ex = make(leverage=2, funding={})
        await market_buy(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(start - H, "100", "100", "100", "100"))
        await ex.on_closed_bar("BTCUSDT", bar(start + H, "100", "100", "100", "100"))
        assert ex.get_account_snapshot().funding_missing == 0


class TestLiquidation:
    async def test_a_long_liquidates_at_the_maintenance_boundary_and_halts(self) -> None:
        ex = make(leverage=2, mmr="0.05")
        await market_buy(ex, "1", "100")
        margin_before = pos(ex).margin
        liq = (D("1") * D("100") - margin_before) / (D("1") * (D("1") - D("0.05")))
        fills = await ex.on_closed_bar(
            "BTCUSDT",
            bar(datetime(2026, 1, 5, 9, 0, tzinfo=UTC), "100", "100", str(liq), "100"),
        )
        [fill] = fills
        assert fill.reason == "liquidation"
        assert fill.side == "SELL"
        assert fill.price == liq
        snap = ex.get_account_snapshot()
        assert snap.liquidations == 1
        assert snap.base_totals == {"BTC": ZERO}
        assert snap.margin_used == ZERO
        assert snap.wallet_available == D("1000") - D("50") - D("0.04")
        # the whole isolated margin is lost: net == -margin
        assert D(str(fill.net_pnl)) == -margin_before
        assert snap.halted is not None and "liquidated" in snap.halted
        with pytest.raises(PaperSessionHaltedError):
            await market_buy(ex, "1", "100")
        assert await ex.flatten() == []  # the position is gone; flatten still runs

    async def test_a_short_liquidates_when_price_rises_through_the_boundary(self) -> None:
        ex = make(leverage=2, mmr="0.05")
        await market_sell(ex, "1", "100")
        margin_before = pos(ex).margin
        liq = (D("1") * D("100") + margin_before) / (D("1") * (D("1") + D("0.05")))
        fills = await ex.on_closed_bar(
            "BTCUSDT",
            bar(datetime(2026, 1, 5, 9, 0, tzinfo=UTC), "100", str(liq), "100", str(liq)),
        )
        [fill] = fills
        assert (fill.reason, fill.side, fill.price) == ("liquidation", "BUY", liq)
        assert D(str(fill.net_pnl)) == -margin_before

    async def test_funding_drains_margin_and_moves_the_liquidation_price(self) -> None:
        settled = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        ex = make(leverage=2, mmr="0.05", funding={"BTCUSDT": [(settled, D("0.0007"))]})
        await market_buy(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(settled - H, "100", "100", "100", "100"))
        await ex.on_closed_bar("BTCUSDT", bar(settled + H, "100", "100", "100", "104"))
        assert pos(ex).margin == D("50") - D("0.07")
        [position] = await ex.fetch_positions()
        assert position["liquidationPrice"] == float(
            (D("100") - (D("50") - D("0.07"))) / (D("1") - D("0.05"))
        )


class TestLiquidationPersistence:
    async def test_the_liquidation_persists_and_restores_halted(self) -> None:
        pool = RecordingPool()
        ts = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
        ex = PerpPaperTradingExecutor(
            initial_balances={"USDT": D("1000")},
            slippage_pct=ZERO,
            maker_fee=D("0.0004"),
            taker_fee=D("0.0004"),
            leverage=2,
            maintenance_margin_rate=D("0.05"),
            db_pool=pool,
            session_id="liq-1",
            funding_reader=static_funding_reader({}),
        )
        await market_buy(ex, "1", "100")
        liq = (D("100") - D("50")) / (D("1") - D("0.05"))
        await ex.on_closed_bar("BTCUSDT", bar(ts, "100", "100", str(liq), "100"))

        # the terminal state reaches the DB: the halted account, the closed position,
        # the LIQUIDATION fill as a trade row
        state = json.loads(pool.accounts["liq-1"][4])
        assert state["halted"] is not None and "liquidated" in state["halted"]
        assert state["positions"]["BTCUSDT"]["qty"] == "0"
        [row] = [
            args
            for sql, args in pool.committed
            if sql.startswith("INSERT INTO trades") and args[15] == "LIQUIDATION"
        ]
        assert (row[1], row[2], row[6]) == ("SELL", liq, -D("50"))

        # a restart restores it halted: the liquidated position stays closed
        rebuilt = await PerpPaperTradingExecutor.restore(pool, "liq-1")
        assert rebuilt is not None
        snap = rebuilt.get_account_snapshot()
        assert snap.halted is not None and "liquidated" in snap.halted
        assert snap.liquidations == 1
        assert (await rebuilt.get_positions()) == []
        with pytest.raises(PaperSessionHaltedError):
            await market_buy(rebuilt, "1", "100")


class TestFundingMarginFloor:
    async def test_a_funding_wipe_liquidates_honestly_at_the_carried_price(self) -> None:
        # the reviewer's repro: a 2.0 rate on a 1x long at 100 owes 200 on a 100 margin;
        # isolated margin caps the loss at the margin — never a negative margin, never
        # a liquidation above the market booking phantom PnL
        settled = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        ex = make(leverage=1, funding={"BTCUSDT": [(settled, D("2.0"))]})
        await market_buy(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(settled - H, "100", "100", "100", "100"))
        await ex.on_closed_bar("BTCUSDT", bar(settled + H, "100", "100", "100", "100"))
        # the next bar would have liquidated at 201.005 on the carried-negative ledger
        await ex.on_closed_bar("BTCUSDT", bar(settled + 2 * H, "100", "100", "100", "100"))

        snap = ex.get_account_snapshot()
        assert snap.liquidations == 1
        assert snap.halted is not None and "liquidated" in snap.halted
        assert pos(ex).margin == ZERO and pos(ex).qty == ZERO
        funding = next(f for f in ex.get_fills() if f.side == "FUNDING")
        liquidation = next(f for f in ex.get_fills() if f.order_type == "LIQUIDATION")
        # the payment is capped at the margin; the close is at the real carried price
        assert funding.net_pnl == -D("100")
        assert funding.price == D("100")
        assert liquidation.price == D("100")
        assert not any(f.net_pnl is not None and f.net_pnl > 0 for f in ex.get_fills()), (
            "a wiped position can never book a positive PnL"
        )
        # funding + close = exactly the lost margin; the account kept the rest
        assert funding.net_pnl + liquidation.net_pnl == -D("100")
        assert snap.wallet_available == D("1000") - D("100") - D("0.04")
        assert snap.equity == snap.wallet_available

    async def test_a_funding_wipe_with_adverse_mark_loss_exactly_the_margin(self) -> None:
        # carried price 90 (unrealized -10): the payment caps at the equity (90) so the
        # total loss across funding and the honest close is exactly the margin
        settled = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        ex = make(leverage=1, funding={"BTCUSDT": [(settled, D("2.0"))]})
        await market_buy(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(settled - H, "100", "100", "100", "90"))
        await ex.on_closed_bar("BTCUSDT", bar(settled + H, "90", "90", "90", "90"))

        snap = ex.get_account_snapshot()
        assert snap.halted is not None
        funding = next(f for f in ex.get_fills() if f.side == "FUNDING")
        liquidation = next(f for f in ex.get_fills() if f.order_type == "LIQUIDATION")
        assert (funding.net_pnl, funding.price) == (-D("90"), D("90"))
        assert (liquidation.price, liquidation.net_pnl) == (D("90"), -D("10"))
        # funding + close = exactly the lost margin, whichever way it is split
        assert funding.net_pnl + liquidation.net_pnl == -D("100")
        assert snap.wallet_available == D("1000") - D("100") - D("0.04")
        assert pos(ex).margin == ZERO

    async def test_funding_never_values_at_a_live_mark_inside_the_window(self) -> None:
        settled = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)
        ex = make(leverage=2, funding={"BTCUSDT": [(settled, D("0.0001"))]})
        await market_buy(ex, "1", "100")
        await ex.on_closed_bar("BTCUSDT", bar(settled - H, "100", "100", "100", "100"))
        ex.set_mark("BTCUSDT", D("120"))  # a live tick inside the settled window
        await ex.on_closed_bar("BTCUSDT", bar(settled + H, "100", "100", "100", "104"))
        [row] = [f for f in ex.get_fills() if f.side == "FUNDING"]
        # the deterministic close carried into the settlement, never the live 120
        assert row.price == D("100")
        assert row.net_pnl == -D("0.01")
        assert pos(ex).margin == D("50") - D("0.01")


class TestFlipAtomicity:
    async def test_a_market_flip_the_wallet_cannot_fund_is_refused_untouched(self) -> None:
        ex = make(capital="100", leverage=2)
        await market_sell(ex, "1", "100")  # short 1: margin 50, wallet 49.96
        before = ex.get_account_snapshot()
        fills_before = ex.get_fills()
        with pytest.raises(InsufficientBalanceError):
            await market_buy(ex, "3", "100")
        after = ex.get_account_snapshot()
        # the close leg never ran: the ledger is byte-identical to before the attempt
        assert (after.wallet_available, after.margin_used, after.realized_net) == (
            before.wallet_available,
            before.margin_used,
            before.realized_net,
        )
        assert ex.get_fills() == fills_before
        assert (pos(ex).qty, pos(ex).margin) == (-D("1"), D("50"))

    async def test_a_funded_market_flip_executes_atomically(self) -> None:
        ex = make(leverage=2)
        await market_sell(ex, "1", "100")
        fill = await market_buy(ex, "3", "100")
        # the close realized its net (0 gross - 0.04 exit fee - 0.04 entry fee) and the
        # remainder opened: one fill, a long 2, margin 100 posted
        assert D(str(fill["net_pnl"])) == -D("0.08")
        assert (pos(ex).qty, pos(ex).entry_price, pos(ex).margin) == (D("2"), D("100"), D("100"))
        snap = ex.get_account_snapshot()
        assert snap.wallet_available == D("1000") - D("50") - D("0.04") + D("50") - D("0.04") - (
            D("100") + D("0.08")
        )

    async def test_a_resting_flip_the_wallet_cannot_fund_cancels_untouched(self) -> None:
        ex = make(capital="100", leverage=2)
        ts = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
        await ex.create_order("BTCUSDT", "BUY", "STOP_MARKET", D("3"), stop_price=D("105"))
        await market_sell(ex, "1", "100")  # the strategy short under the resting entry
        before = ex.get_account_snapshot()
        fills = await ex.on_closed_bar("BTCUSDT", bar(ts, "100", "106", "99", "106"))
        [cancelled] = fills
        assert (cancelled.status, cancelled.reason) == ("CANCELLED", "insufficient_margin_on_gap")
        after = ex.get_account_snapshot()
        assert (after.wallet_available, after.margin_used, after.realized_net) == (
            before.wallet_available,
            before.margin_used,
            before.realized_net,
        )
        assert (pos(ex).qty, pos(ex).margin) == (-D("1"), D("50"))
        assert ex.get_account_snapshot().open_orders == 0


class TestRestingOrders:
    async def test_the_protective_pair_on_a_long_takes_profit_at_the_trigger(self) -> None:
        ex = make(leverage=2)
        await market_buy(ex, "1", "100")
        resp = await ex.create_order(
            "BTCUSDT", "SELL", "OCO", D("1"), price=D("110"), stop_price=D("95")
        )
        assert resp["take_profit_order_id"] and resp["stop_loss_order_id"]
        fills = await ex.on_closed_bar(
            "BTCUSDT", bar(datetime(2026, 1, 5, 9, 0, tzinfo=UTC), "100", "112", "99", "112")
        )
        [fill] = fills
        assert (fill.reason, fill.price) == ("take_profit", D("110"))
        assert ex.get_account_snapshot().open_orders == 0  # sibling-cancelled

    async def test_a_buy_stop_protects_a_short(self) -> None:
        ex = make(leverage=2)
        await market_sell(ex, "1", "100")
        await ex.create_order(
            "BTCUSDT",
            "BUY",
            "STOP_MARKET",
            D("1"),
            stop_price=D("105"),
            params={"reduce_only": True},
        )
        fills = await ex.on_closed_bar(
            "BTCUSDT", bar(datetime(2026, 1, 5, 9, 0, tzinfo=UTC), "100", "106", "99", "106")
        )
        [fill] = fills
        assert (fill.side, fill.reason) == ("BUY", "stop_loss")
        assert fill.net_pnl is not None

    async def test_state_round_trip_preserves_the_margin_wallet(self) -> None:
        ex = make(leverage=3)
        await market_buy(ex, "1", "100")
        state = ex.to_state()
        rebuilt = PerpPaperTradingExecutor.from_state(state)
        assert rebuilt.get_account_snapshot().leverage == 3
        a, b = ex.get_account_snapshot(), rebuilt.get_account_snapshot()
        assert (b.wallet_available, b.margin_used, b.equity, b.base_totals) == (
            a.wallet_available,
            a.margin_used,
            a.equity,
            a.base_totals,
        )
        assert rebuilt.to_state() == state


def test_the_default_maintenance_rate_is_the_lowest_tier() -> None:
    assert D("0.005") == MAINTENANCE_MARGIN_RATE
