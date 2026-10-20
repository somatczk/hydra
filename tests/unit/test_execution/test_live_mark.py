"""Live PriceEvent -> paper executor mark on the real session seam (P1-E A7).

Harness account: 1000 USDT, history closes at 100, 0.1 % slippage and fee. A manual MARKET order
fills at the latest PriceEvent since the last closed bar; strategies, stops and limits still see
closed bars only.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from hydra.core.types import OrderType, Side
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    held,
    rule,
    usdt,
    write_strategy,
)

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal
ENTRY_AT_100 = rule(("less_than", 100.5), ("greater_than", 99.5))


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


async def _buy(h: Harness, qty: str = "0.5") -> None:
    assert h.mgr is not None
    _, result = await h.mgr.submit_manual_order(symbol="BTCUSDT", side=Side.BUY, quantity=D(qty))
    assert result.status == "FILLED"


class TestManualMarketFill:
    async def test_manual_buy_fills_at_the_latest_price_event(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "102")
        await h.price(session, "103")

        await _buy(h)

        (fill,) = executor(session).get_fills()
        assert (fill.price, fill.fee, fill.mark_source) == (D("103.103"), D("0.0515515"), "live")
        assert await usdt(session) == D("948.3969485")  # 1000 - 51.5515 - 0.0515515
        assert await executor(session).get_equity() == D("998.3969485")  # BTC still at close 100

    async def test_without_a_price_event_the_fill_falls_back_to_the_close(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))

        await _buy(h)

        (fill,) = executor(session).get_fills()
        assert (fill.price, fill.mark_source) == (D("100.100"), "last_close")

    async def test_foreign_symbol_or_exchange_price_is_ignored(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "103", symbol="ETHUSDT")
        await h.price(session, "103", exchange_id="kraken")

        await _buy(h)

        assert executor(session).get_fills()[0].price == D("100.100")

    async def test_manual_exit_sells_at_the_live_price(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await _buy(h, "1")
        await h.price(session, "97")
        assert h.mgr is not None

        _, result = await h.mgr.submit_manual_order(symbol="BTCUSDT", side=Side.SELL)

        assert result.status == "FILLED"
        sell = executor(session).get_fills()[-1]
        assert (sell.side, sell.price, sell.mark_source) == ("SELL", D("96.903"), "live")
        assert await held(session) == 0


class TestClosedBarContractUnchanged:
    async def test_strategy_entry_fills_at_the_bar_close_not_an_earlier_tick(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "rt", entry_long=ENTRY_AT_100))
        await h.price(session, "103")

        await h.bar(session, "100")

        (fill,) = executor(session).get_fills()
        assert (fill.price, fill.mark_source) == (D("100.100"), "last_close")

    async def test_price_event_never_triggers_a_stop_or_a_limit(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await _buy(h, "1")
        assert h.mgr is not None
        _, limit = await h.mgr.submit_manual_order(
            symbol="BTCUSDT",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=D("0.5"),
            price=D("94"),
        )
        assert limit.status == "SUBMITTED"

        await h.price(session, "90")

        assert [f.side for f in executor(session).get_fills()] == ["BUY"]
        assert await held(session) == D("1")
        assert len(await executor(session).fetch_open_orders("BTCUSDT")) == 2

        await h.bar(session, "90", open="90")

        fills = sorted((f.side, f.reason, f.price) for f in executor(session).get_fills()[1:])
        assert fills == [("BUY", "limit", D("90")), ("SELL", "stop_loss", D("89.910"))]


class TestKillSwitch:
    async def test_kill_switch_flattens_at_the_live_price(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await _buy(h, "1")
        await h.price(session, "97")
        assert h.mgr is not None

        report = await h.mgr.stop_all(actor="test", reason="kill switch test")

        assert report.positions_remaining == 0
        sell = executor(session).get_fills()[-1]
        assert (sell.side, sell.price, sell.mark_source) == ("SELL", D("96.903"), "live")
