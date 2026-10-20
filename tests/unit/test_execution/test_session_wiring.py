"""Session wiring: closed bar -> paper executor -> strategy -> OrderManager -> fill feedback.

Expected numbers follow P1-D's ledger: market fills at the last close x (1 +/- 0.001) slippage,
0.1 % fee in USDT on each fill, net PnL = gross - exit fee - pro-rata entry fee. Entries are sized
by RiskService.size_entry: sleeve 1000 x max_position_pct 10 % = 100 USDT at mark 100 -> 1 BTC.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from prometheus_client import REGISTRY

from hydra.core.events import BarEvent, FeedStatusEvent, OrderFillEvent
from hydra.core.types import OHLCV, Direction, Symbol, Timeframe
from hydra.strategy.builtin.rule_based import RuleBasedStrategy
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
ALWAYS = rule(("greater_than", 0.0))


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


def _fills(session: Any) -> list[tuple[str, Decimal, Decimal]]:
    return [(f.side, f.quantity, f.price) for f in executor(session).get_fills()]


class TestRoundTrip:
    async def test_entry_then_exit_round_trip_arithmetic(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(
            strategies_dir, "rt", entry_long=ENTRY_AT_100, exit_long=rule(("greater_than", 105))
        )
        session = await h.session(sid)
        runtime = session._runtime
        assert runtime is not None
        seen: list[OrderFillEvent] = []

        async def record(event: Any) -> None:
            seen.append(event)

        await runtime.bus.subscribe("order_fill", record)

        await h.bar(session, "100")
        assert _fills(session) == [("BUY", D("1.00000"), D("100.100"))]
        assert await usdt(session) == D("899.7999")  # 1000 - 100.1 - fee 0.1001
        pos = runtime.context.position("BTCUSDT")
        assert pos is not None and (pos.direction, pos.quantity) == (Direction.LONG, D("1"))
        assert runtime.context.portfolio_value() == D("999.7999")  # USDT + 1 BTC at 100

        await h.bar(session, "110")
        assert _fills(session)[1] == ("SELL", D("1.00000"), D("109.890"))
        assert await usdt(session) == D("1009.58001")  # + 109.89 - fee 0.10989
        assert runtime.context.position("BTCUSDT") is None
        assert [e.realized_pnl for e in seen] == [None, D("9.58001")]
        assert seen[1].gross_pnl == D("9.79")
        assert seen[1].reduce_only and seen[1].tag == "exit"
        snap = executor(session).get_account_snapshot()
        assert snap.equity == snap.initial_equity + snap.realized_net == D("1009.58001")

    async def test_first_bar_has_a_price_before_the_strategy_runs(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(strategies_dir, "first", entry_long=rule(("less_than", 95.0)))
        session = await h.session(sid)
        await h.bar(session, "90", open="100", high="100", low="90")
        # the strategy saw close 90 and the entry filled at that same close x 1.001
        assert _fills(session) == [("BUY", D("1.11111"), D("90.090"))]

    async def test_strategy_on_fill_is_called(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sid = write_strategy(strategies_dir, "onfill", entry_long=ENTRY_AT_100)
        session = await h.session(sid)
        runtime = session._runtime
        assert runtime is not None
        strategy = runtime.engine.get_strategy(sid)
        assert isinstance(strategy, RuleBasedStrategy)
        calls: list[OrderFillEvent] = []

        async def on_fill(fill: OrderFillEvent) -> None:
            calls.append(fill)

        monkeypatch.setattr(strategy, "on_fill", on_fill)
        await h.bar(session, "100")
        assert [(c.side.value, c.quantity) for c in calls] == [("BUY", D("1.00000"))]


class TestEntryGating:
    async def test_repeated_entry_signals_do_not_pyramid(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "pyr", entry_long=ALWAYS))
        for close in ("100", "101", "102", "103"):
            await h.bar(session, close)
        assert [f[0] for f in _fills(session)] == ["BUY"]
        assert await held(session) == D("1.00000")

    async def test_short_entry_on_spot_is_dropped_and_counted(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(strategies_dir, "shorty", entry_short=ALWAYS)
        session = await h.session(sid)
        before = REGISTRY.get_sample_value(
            "hydra_spot_short_ignored_total",
            {"strategy_id": "shorty", "signal_type": "entry_short"},
        )
        for close in ("100", "99"):
            await h.bar(session, close)
        after = REGISTRY.get_sample_value(
            "hydra_spot_short_ignored_total",
            {"strategy_id": "shorty", "signal_type": "entry_short"},
        )
        assert _fills(session) == []
        assert await usdt(session) == D("1000")
        assert (after or 0) - (before or 0) == 2
        assert session._runtime is not None and session._runtime.spot_shorts_ignored == 2

    async def test_feed_stale_pauses_entries_until_every_scope_is_live(
        self, h: Harness, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "stale", entry_long=ALWAYS))
        runtime = session._runtime
        assert runtime is not None
        reasons: dict[str, list[str]] = {"*": ["ws_silence"], "BTCUSDT/1h": ["bar_overdue"]}
        monkeypatch.setattr(runtime.feed, "stale_reasons", lambda _eid: dict(reasons))
        monkeypatch.setattr(runtime.feed, "is_live", lambda _eid: not reasons)

        await runtime.bus.publish(FeedStatusEvent(status="stale", reason="ws_silence"))
        await h.bar(session, "100")
        assert _fills(session) == []
        assert runtime.entries_paused is not None and "ws_silence" in runtime.entries_paused

        del reasons["*"]
        await runtime.bus.publish(FeedStatusEvent(status="live"))
        await h.bar(session, "100")
        assert _fills(session) == []  # the stream scope is still stale

        reasons.clear()
        await runtime.bus.publish(FeedStatusEvent(status="live"))
        await h.bar(session, "100")
        assert [f[0] for f in _fills(session)] == ["BUY"]


class TestBarRouting:
    async def test_replay_and_other_timeframes_do_not_reach_the_executor(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(
            strategies_dir,
            "mtf",
            entry_long=ENTRY_AT_100,
            timeframes={"primary": "1h", "confirmation": "4h"},
        )
        session = await h.session(sid)
        runtime = session._runtime
        assert runtime is not None
        ex = executor(session)
        primed = ex.get_account_snapshot().last_bar_ts["BTCUSDT"]

        await h.bar(session, "120", timeframe=Timeframe.H4)
        assert ex.get_account_snapshot().last_bar_ts["BTCUSDT"] == primed
        assert ex.get_account_snapshot().marks["BTCUSDT"] == D("100.0")

        replayed = OHLCV(
            open=D("100"),
            high=D("100"),
            low=D("100"),
            close=D("100"),
            volume=D("1"),
            timestamp=primed + timedelta(hours=1),
        )
        await runtime.bus.publish(
            BarEvent(symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=replayed, replay=True)
        )
        assert ex.get_account_snapshot().last_bar_ts["BTCUSDT"] == primed
        assert _fills(session) == []

        await h.bar(session, "100")
        assert [f[0] for f in _fills(session)] == ["BUY"]

    async def test_position_gauge_follows_fills(self, h: Harness, strategies_dir: Path) -> None:
        sid = write_strategy(
            strategies_dir, "gauge", entry_long=ENTRY_AT_100, exit_long=rule(("greater_than", 105))
        )
        session = await h.session(sid)
        labels = {
            "session_id": session.session_id,
            "strategy_id": "gauge",
            "exchange_id": "binance",
            "symbol": "BTCUSDT",
        }
        await h.bar(session, "100")
        assert REGISTRY.get_sample_value("hydra_position_size", labels) == 1.0
        await h.bar(session, "110")
        assert REGISTRY.get_sample_value("hydra_position_size", labels) == 0.0
