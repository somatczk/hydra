"""The live session seams: the wiring's poller and coverage producer, the signal ladder,
the kill-switch sweep and the both-scopes latch — real SessionManager, OrderManager and
runtime over the fake exchange (no DB, no real keys).

The outbox/ledger/poller numbers live in ``test_live_executor.py``; this file pins the
session-level contracts P3-B and the wiring consume.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import EntrySignal
from hydra.core.types import Direction, Symbol
from hydra.execution.errors import ExchangeRateLimitedError, SubmitUnknownError
from hydra.execution.live_executor import LiveOrderPoller, LiveTradingExecutor
from hydra.execution.order_manager import OrderManager
from hydra.execution.session_recovery import TradingSession
from hydra.execution.session_wiring import SessionRuntime
from hydra.risk.market_rules import MarketRules
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore
from tests.fixtures.binance_trading import FakeBinanceTradingClient
from tests.unit.test_execution.session_harness import Harness, write_strategy

D = Decimal

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
)
ETH_RULES = MarketRules(
    symbol="ETHUSDT",
    amount_step=D("0.0001"),
    min_qty=D("0.0001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
)


def _live_executor(
    fake: FakeBinanceTradingClient, session_id: str, symbols: tuple[str, ...] = ("BTCUSDT",)
) -> LiveTradingExecutor:
    rules = {"BTCUSDT": RULES, "ETHUSDT": ETH_RULES}
    ex = LiveTradingExecutor(
        fake,
        exchange_id="binance",
        initial_balances={"USDT": D("10000")},
        db_pool=None,
        strategy_id="strat",
        session_id=session_id,
        market_rules={s: rules[s] for s in symbols},
        poll_interval=0.05,
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", D("100000"))
    ex.set_mark("ETHUSDT", D("1000"))
    return ex


async def _runtime(
    h: Harness, strategies_dir: Path, fake: FakeBinanceTradingClient, *, symbols=("BTCUSDT",)
) -> tuple[SessionRuntime, LiveTradingExecutor, list[str]]:
    sid = write_strategy(strategies_dir, "live-rt", symbols=list(symbols))
    assert h.mgr is not None and h.mgr.risk is not None
    cfg = h.mgr._find_strategy_config(sid)
    session = TradingSession(
        session_id="live-1",
        strategy_id=sid,
        trading_mode="live",
        exchange_id="binance",
        symbols=list(symbols),
        timeframe="1h",
        paper_capital=D("10000"),
    )
    session.status = "running"
    executor = _live_executor(fake, "live-1", symbols)
    failures: list[str] = []

    async def on_failure(_session: TradingSession, reason: str) -> None:
        failures.append(reason)

    runtime = SessionRuntime(
        session,
        cfg,
        executor=executor,
        risk=h.mgr.risk,
        clients=h.clients(),
        hydra_config=HydraConfig(),
        db_pool=None,
        required_history=2,
        on_failure=on_failure,
    )
    await h.mgr.risk.register_session(
        "live-1",
        executor,
        allocated=D("10000"),
        symbols=list(symbols),
        exchange_id="binance",
        strategy_id=sid,
    )
    await h.mgr.risk.on_mark("binance", "BTCUSDT", D("100000"), datetime(2026, 10, 27, tzinfo=UTC))
    return runtime, executor, failures


async def _filled_position(runtime: SessionRuntime, executor: LiveTradingExecutor) -> None:
    await executor.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
    assert runtime._poller is not None
    await runtime._poller.sweep()
    assert any(p.quantity > 0 for p in await executor.get_positions("BTCUSDT"))


# ---------------------------------------------------------------------------
# The wiring: poller lifecycle and the coverage producer (R4)
# ---------------------------------------------------------------------------


async def test_wire_starts_the_poller_and_shutdown_stops_it(
    strategies_dir: Path,
) -> None:
    h = Harness(strategies_dir)
    await h.start()
    try:
        fake = FakeBinanceTradingClient()
        runtime, _, _ = await _runtime(h, strategies_dir, fake)
        await runtime.build()
        await runtime.wire()
        assert runtime._poller is not None
        assert runtime._poller._task is not None and not runtime._poller._task.done()

        await runtime.shutdown()
        assert runtime._poller is None
    finally:
        await h.close()


async def test_the_coverage_producer_reports_per_symbol_none_when_not_held(
    strategies_dir: Path,
) -> None:
    h = Harness(strategies_dir)
    await h.start()
    try:
        fake = FakeBinanceTradingClient()
        runtime, executor, _ = await _runtime(h, strategies_dir, fake)
        await runtime.build()
        await runtime.wire()
        try:
            states = await runtime._live_coverage()
            assert states == {"BTCUSDT": None}  # nothing held: the gauge clears

            # the entry fill auto-arms the protective stop through the runtime's bus
            await _filled_position(runtime, executor)
            states = await runtime._live_coverage()
            assert states["BTCUSDT"] is not None
            assert states["BTCUSDT"].state == "confirmed"
            assert states["BTCUSDT"].kind == "stop"

            # disarmed while still holding: the held symbol reads missing, loudly
            await runtime.protect.disarm("BTCUSDT")
            states = await runtime._live_coverage()
            assert states["BTCUSDT"] is not None
            assert states["BTCUSDT"].state == "missing"
        finally:
            await runtime.shutdown()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# The signal ladder: an unknown submit skips the order, never kills the session
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [
        SubmitUnknownError("hyd-1", "submit outcome unknown"),
        ExchangeRateLimitedError("binance refused the call"),
    ],
)
async def test_signal_submits_with_unknown_outcomes_skip_the_order(
    strategies_dir: Path, error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    h = Harness(strategies_dir)
    await h.start()
    try:
        fake = FakeBinanceTradingClient()
        runtime, executor, failures = await _runtime(h, strategies_dir, fake)
        await runtime.build()
        await runtime.wire()
        try:
            real = executor.create_order

            async def broken(*args: Any, **kwargs: Any) -> dict[str, Any]:
                raise error

            executor.create_order = broken  # type: ignore[method-assign]
            with caplog.at_level(logging.WARNING, logger="hydra.execution.session_wiring"):
                await runtime.bus.publish(
                    EntrySignal(
                        symbol=Symbol("BTCUSDT"),
                        direction=Direction.LONG,
                        strategy_id=runtime.cfg.id,
                        exchange_id="binance",
                    )
                )
                for _ in range(10):
                    await asyncio.sleep(0)
            assert failures == []  # never the session
            assert runtime.session.status == "running"
            assert runtime.om.get_open_orders() == []
            assert executor.get_account_snapshot().base_totals == {}
            assert caplog.records, "the skip must be logged"
        finally:
            await runtime.shutdown()
            executor.create_order = real  # type: ignore[method-assign]
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# The kill-switch sweep and the both-scopes latch
# ---------------------------------------------------------------------------


async def test_the_sweep_cancels_all_orders_once_per_traded_symbol(
    strategies_dir: Path,
) -> None:
    """A live session with a position in BTC only: one cancel-all, on BTCUSDT (A9)."""
    h = Harness(strategies_dir)
    await h.start()
    mgr = h.mgr
    assert mgr is not None
    try:
        fake = FakeBinanceTradingClient()
        executor = _live_executor(fake, "live-1", symbols=("BTCUSDT", "ETHUSDT"))
        session = TradingSession(
            session_id="live-1",
            strategy_id="live-rt",
            trading_mode="live",
            exchange_id="binance",
            symbols=["BTCUSDT", "ETHUSDT"],
            timeframe="1h",
            paper_capital=D("10000"),
        )
        session.status = "running"
        session._executor = executor
        mgr._sessions[session.session_id] = session

        await executor.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"))
        poller = LiveOrderPoller(
            executor,
            OrderManager(executor, InMemoryEventBus(), session_id="live-1"),
            interval=0.05,
            resolve_grace=0.0,
        )
        await poller.sweep()
        assert any(p.quantity > 0 for p in await executor.get_positions("BTCUSDT"))
        await executor.create_order(
            "BTCUSDT", "SELL", "STOP_MARKET", D("0.00999"), stop_price=D("95000")
        )

        # the flatten sell cannot go out: the position remains, honestly reported
        fake.fail_create = "rate_limit"
        report = await mgr.stop_all(actor="test", reason="kill switch")

        assert fake.cancel_all_calls == ["BTCUSDT"]  # ETH was never traded: not swept
        assert report.sessions_stopped == 1
        assert report.positions_remaining == 1
        assert "could NOT be closed" in report.flatten_failures[0] or report.flatten_failures
        assert session.status == "stopped"
        assert session.positions_at_stop == 1
    finally:
        await h.close()


async def test_stop_all_latches_both_scopes_and_audits_each(strategies_dir: Path) -> None:
    h = Harness(strategies_dir)
    await h.start()
    mgr = h.mgr
    assert mgr is not None and h.store is not None
    live_store = InMemoryRiskStateStore(paper_capital=D("5000"))
    mgr.live_risk = RiskService(live_store, scope="live", market_rules=mgr._market_rules)
    assert mgr._build_live_risk() is mgr.live_risk
    await mgr.live_risk.load()
    await mgr.live_risk.recovery_complete()
    try:
        report = await mgr.stop_all(actor="test", reason="drill")
        assert report.active is True
        assert mgr.risk is not None and mgr.risk.kill_switch_active()
        assert mgr.live_risk.kill_switch_active()
        assert mgr.kill_switch_active()
        assert [e.kind for e in h.store.events] == ["kill_switch_on"]  # scope 'paper'
        assert [e.kind for e in live_store.events] == ["kill_switch_on"]  # scope 'live'
        assert all(e.scope == "live" for e in live_store.events)

        await mgr.release_kill_switch(actor="test", reason="drill over")
        assert mgr.risk is not None and not mgr.risk.kill_switch_active()
        assert not mgr.live_risk.kill_switch_active()
    finally:
        await h.close()


# ---------------------------------------------------------------------------
# The runtime flatten drains the landed fills (flatten_session's live hook)
# ---------------------------------------------------------------------------


async def test_runtime_flatten_reports_zero_remaining_for_a_live_session(
    strategies_dir: Path,
) -> None:
    h = Harness(strategies_dir)
    await h.start()
    try:
        fake = FakeBinanceTradingClient()
        runtime, executor, _ = await _runtime(h, strategies_dir, fake)
        await runtime.build()
        await runtime.wire()
        try:
            await _filled_position(runtime, executor)
            await executor.create_order(
                "BTCUSDT", "SELL", "STOP_MARKET", D("0.00999"), stop_price=D("95000")
            )
            assert runtime.session.positions_at_stop == 0

            result = await runtime.flatten()

            assert result.positions_remaining == 0
            assert not any(p.quantity > 0 for p in await executor.get_positions("BTCUSDT"))
            assert fake.open_orders_raw() == []  # the resting stop was cancelled
        finally:
            await runtime.shutdown()
    finally:
        await h.close()
