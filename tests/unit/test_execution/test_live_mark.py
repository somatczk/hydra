"""Live PriceEvent -> paper executor mark on the real session seam (P1-E A7).

Harness account: 1000 USDT, history closes at 100, 0.1 % slippage and fee, 5 % default stop. A
manual MARKET order fills at the latest PriceEvent since the last closed bar; strategies, stops and
limits still see closed bars only, and an order placed at a live mark sees only the move from that
mark to the close of its bar. Risk breakers wait for the next closed bar after such a fill.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest

from hydra.core.events import Event, FeedStatusEvent, OrderFillEvent
from hydra.core.types import OrderType, Side
from hydra.execution.protective_orders import ProtectionSpec
from hydra.execution.session_manager import TradingSession
from hydra.execution.session_wiring import ManualOrderResult
from hydra.risk.pretrade import RiskConfig
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


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


async def _buy(
    h: Harness, qty: str = "0.5", protection: ProtectionSpec | None = None
) -> ManualOrderResult:
    assert h.mgr is not None
    _, result = await h.mgr.submit_manual_order(
        symbol="BTCUSDT", side=Side.BUY, quantity=D(qty), protection=protection
    )
    assert result.status == "FILLED"
    return result


async def _sell(h: Harness) -> ManualOrderResult:
    assert h.mgr is not None
    _, result = await h.mgr.submit_manual_order(symbol="BTCUSDT", side=Side.SELL)
    assert result.status == "FILLED"
    return result


async def _resting(session: TradingSession) -> list[tuple[str, float | None]]:
    orders = await executor(session).fetch_open_orders("BTCUSDT")
    return [(o["type"], o["stop_price"]) for o in orders]


class TestManualMarketFill:
    async def test_manual_buy_fills_at_the_latest_price_event(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "102")
        await h.price(session, "103")

        result = await _buy(h)

        (fill,) = executor(session).get_fills()
        assert (fill.price, fill.fee, fill.mark_source) == (D("103.103"), D("0.0515515"), "live")
        assert (result.fill_price, result.mark_source) == (D("103.103"), "live")
        assert await usdt(session) == D("948.3969485")  # 1000 - 51.5515 - 0.0515515
        assert await executor(session).get_equity() == D("998.3969485")  # BTC still at close 100

    async def test_without_a_price_event_the_fill_falls_back_to_the_close(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))

        result = await _buy(h)

        (fill,) = executor(session).get_fills()
        assert (fill.price, fill.mark_source) == (D("100.100"), "last_close")
        assert (result.fill_price, result.mark_source) == (D("100.100"), "last_close")

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

        result = await _sell(h)

        sell = executor(session).get_fills()[-1]
        assert (sell.side, sell.price, sell.mark_source) == ("SELL", D("96.903"), "live")
        assert (result.fill_price, result.mark_source) == (D("96.903"), "live")
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

        published: list[OrderFillEvent] = []

        async def record(event: Event) -> None:
            if isinstance(event, OrderFillEvent):
                published.append(event)

        assert session._event_bus is not None
        await session._event_bus.subscribe("order_fill", record)

        report = await h.mgr.stop_all(actor="test", reason="kill switch test")
        await settle()

        assert report.positions_remaining == 0
        sell = executor(session).get_fills()[-1]
        assert (sell.side, sell.price, sell.mark_source) == ("SELL", D("96.903"), "live")
        assert [(e.side, e.price, e.mark_source) for e in published] == [
            (Side.SELL, D("96.903"), "live")
        ]


class TestEntryBarAfterALiveFill:
    """The bar a live-mark entry was placed in opened at the old close, before the entry."""

    async def test_open_below_the_stop_before_the_entry_does_not_stop_it_out(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "106")
        await _buy(h)
        assert await _resting(session) == [("STOP_MARKET", 100.8007)]  # 106.106 x 0.95

        await h.bar(session, "106", open="100", low="100", high="106")

        assert [f.side for f in executor(session).get_fills()] == ["BUY"]
        assert await held(session) == D("0.5")
        assert await _resting(session) == [("STOP_MARKET", 100.8007)]

    async def test_a_close_through_the_stop_fills_at_the_stop(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "106")
        await _buy(h)

        await h.bar(session, "100.5", open="100", low="100", high="106")

        sell = executor(session).get_fills()[-1]
        assert (sell.side, sell.reason, sell.price) == ("SELL", "stop_loss", D("100.6998993"))
        assert await held(session) == 0

    async def test_from_the_next_bar_the_full_range_and_gap_rule_apply(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "106")
        await _buy(h)
        await h.bar(session, "106", open="100", low="100", high="106")

        await h.bar(session, "99", open="99")

        sell = executor(session).get_fills()[-1]
        assert (sell.side, sell.reason, sell.price) == ("SELL", "stop_loss", D("98.901"))

    async def test_trailing_stop_seeded_at_the_live_mark_ignores_the_earlier_open(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        await h.price(session, "106")
        await _buy(h, protection=ProtectionSpec(trailing_pct=D("0.05")))

        await h.bar(session, "106", open="100", low="100", high="106")

        assert [f.side for f in executor(session).get_fills()] == ["BUY"]
        assert await _resting(session) == [("TRAILING_STOP", None)]

        await h.bar(session, "100", open="106", high="110", low="100")

        sell = executor(session).get_fills()[-1]
        assert (sell.reason, sell.price) == ("trailing_stop", D("104.3955"))  # 110 x 0.95 x 0.999


class TestStaleFeed:
    async def test_a_fill_on_a_mark_from_before_the_feed_went_stale_is_labelled(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        runtime = session._runtime
        assert runtime is not None
        await _buy(h, "1")
        await h.price(session, "97")
        live = {"value": False}
        monkeypatch.setattr(runtime.feed, "is_live", lambda _eid: live["value"])

        await runtime.bus.publish(FeedStatusEvent(status="stale", reason="ws_silence"))
        await settle()
        result = await _sell(h)

        assert (result.fill_price, result.mark_source) == (D("96.903"), "stale_live")

    async def test_a_fresh_price_after_the_outage_is_live_again(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "manual"))
        runtime = session._runtime
        assert runtime is not None
        await _buy(h, "1")
        await h.price(session, "97")
        live = {"value": False}
        monkeypatch.setattr(runtime.feed, "is_live", lambda _eid: live["value"])
        await runtime.bus.publish(FeedStatusEvent(status="stale", reason="ws_silence"))
        await settle()

        live["value"] = True
        await runtime.bus.publish(FeedStatusEvent(status="live"))
        await h.price(session, "98")
        result = await _sell(h)

        assert (result.fill_price, result.mark_source) == (D("97.902"), "live")


class TestRiskWaitsForTheClosedBar:
    """Cash moves at the live price while risk values the position at the last close."""

    @pytest.fixture()
    async def h(self, strategies_dir: Path) -> AsyncIterator[Harness]:
        harness = Harness(
            strategies_dir,
            risk_config=RiskConfig(
                max_position_pct=D("1"),
                max_portfolio_heat=D("1"),
                max_risk_per_trade=D("1"),
                max_liquidity_pct=D("1"),
            ),
        )
        await harness.start()
        yield harness
        await harness.close()

    @pytest.mark.parametrize(
        ("live", "fill_price", "equity_after_bar"),
        [("104", D("104.104"), D("998.127064")), ("90", D("90.090"), D("998.37919"))],
    )
    async def test_a_live_fill_books_no_phantom_pnl(
        self,
        h: Harness,
        strategies_dir: Path,
        live: str,
        fill_price: Decimal,
        equity_after_bar: Decimal,
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "big", max_position_pct=100.0))
        assert h.mgr is not None and h.mgr.risk is not None
        risk = h.mgr.risk
        await h.price(session, live)

        result = await _buy(h, "9")

        assert result.fill_price == fill_price
        before = risk.account()
        assert (before.equity, before.peak_equity, before.tier) == (D("1000"), D("1000"), 0)
        assert not before.restrictions.daily_halt

        await h.bar(session, live)

        after = risk.account()
        assert (after.equity, after.peak_equity, after.tier) == (equity_after_bar, D("1000"), 0)
        assert after.drawdown == (D("1000") - equity_after_bar) / D("1000")
        assert not after.restrictions.daily_halt
