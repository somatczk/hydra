"""Recovery through the real lifespan step: paper resumes with a live task, nothing is a zombie.

The database is faked at its boundary: a pool answering the trading_sessions queries, the paper
state store (``PaperTradingExecutor.restore``) and the risk state store. Everything above runs for
real, including the feed warm-up, the strategy engine and the protective orders.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from hydra.core.config import HydraConfig
from hydra.core.types import OHLCV, Direction
from hydra.dashboard import lifespan
from hydra.execution import session_manager as sm
from hydra.execution.errors import DuplicateSessionError
from hydra.execution.paper_trading import PaperStateUnavailableError, PaperTradingExecutor
from hydra.execution.session_manager import PRE_P1_STATE, SessionManager
from hydra.risk.state import InMemoryRiskStateStore
from tests.unit.test_execution.session_harness import Harness, settle, write_strategy

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal


class _Conn:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.persisted: list[tuple[Any, ...]] = []

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "status = 'running'" in query:
            return [r for r in self.rows if r["status"] == "running"]
        if "LIMIT 50" in query:
            return list(self.rows)
        return []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        return None

    async def execute(self, query: str, *args: Any) -> str:
        if "INSERT INTO trading_sessions" in query:
            self.persisted.append(args)
            for row in self.rows:
                if row["id"] == args[0]:
                    row["status"], row["error_message"] = args[3], args[10]
        return "UPDATE 1"


class _Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.conn = _Conn(rows)

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_Conn]:
        yield self.conn


def _row(
    sid: str,
    strategy_id: str,
    *,
    mode: str = "paper",
    exchange: str = "binance",
    created_by: str | None = None,
) -> dict:
    return {
        "id": sid,
        "strategy_id": strategy_id,
        "trading_mode": mode,
        "status": "running",
        "exchange_id": exchange,
        "symbols": ["BTCUSDT"],
        "timeframe": "1h",
        "paper_capital": D("1000"),
        "started_at": datetime(2026, 10, 1, tzinfo=UTC),
        "stopped_at": None,
        "error_message": None,
        "positions_at_stop": 0,
        "created_by": created_by,
    }


def _bar(close: str, ts: datetime) -> OHLCV:
    c = D(close)
    return OHLCV(open=c, high=c, low=c, close=c, volume=D("1"), timestamp=ts)


async def _account_with_long(end: datetime, *, stop: str | None = None) -> dict[str, Any]:
    """The state a session persisted before the restart: 0.05 BTC bought at 100.1."""
    ex = PaperTradingExecutor(initial_balances={"USDT": D("1000")}, session_id="s1")
    await ex.on_closed_bar("BTCUSDT", _bar("100", end - timedelta(hours=3)))
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.05"))
    if stop is not None:
        await ex.create_order("BTCUSDT", "SELL", "STOP_MARKET", D("0.05"), stop_price=D(stop))
    return ex.to_state()


@pytest.fixture()
def restore_from(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    """Paper state by session id: a state dict, None (pre-P1) or an exception to raise."""
    saved: dict[str, Any] = {}

    async def restore(
        cls: type[PaperTradingExecutor], db_pool: Any, session_id: str, *, market_rules: Any = None
    ) -> PaperTradingExecutor | None:
        value = saved.get(session_id)
        if isinstance(value, Exception):
            raise value
        if value is None:
            return None
        return PaperTradingExecutor.from_state(
            value, session_id=session_id, strategy_id="paper-a", market_rules=market_rules
        )

    monkeypatch.setattr(PaperTradingExecutor, "restore", classmethod(restore))
    yield saved


async def _boot(h: Harness, rows: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch) -> Any:
    store = InMemoryRiskStateStore(paper_capital=D("1000"))
    monkeypatch.setattr(sm, "PgRiskStateStore", lambda pool, **kwargs: store)
    app = FastAPI()
    app.state.db_pool = _Pool(rows)
    await lifespan._init_session_manager(app, HydraConfig(), h.clients())
    mgr: SessionManager = app.state.session_manager
    h.mgr = mgr
    await settle()
    return mgr, app.state.db_pool.conn


async def test_recovery_resumes_paper_and_errors_live_rows_without_credentials(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(strategies_dir)
    write_strategy(strategies_dir, "paper-a")
    write_strategy(strategies_dir, "live-b")
    write_strategy(strategies_dir, "bybit-c", exchange_id="bybit")
    write_strategy(strategies_dir, "old-d")
    write_strategy(strategies_dir, "broken-e")
    restore_from["s1"] = await _account_with_long(h.end)
    restore_from["s5"] = PaperStateUnavailableError("state row unreadable")
    rows = [
        _row("s1", "paper-a", created_by="service:mcp"),
        _row("s2", "live-b", mode="live"),
        _row("s3", "bybit-c", exchange="bybit"),
        _row("s4", "old-d"),
        _row("s5", "broken-e"),
    ]
    mgr, _ = await _boot(h, rows, monkeypatch)
    try:
        sessions = {s.session_id: s for s in mgr.list_sessions()}
        s1 = sessions["s1"]
        assert s1.status == "running" and s1.task_alive
        assert s1.created_by == "service:mcp"  # the attribution survives the restart
        assert s1._runtime is not None and s1._runtime.engine._running
        assert sessions["s2"].status == "error"
        assert "live setup failed" in (sessions["s2"].error_message or "")
        assert not sessions["s2"].task_alive
        assert sessions["s3"].status == "error" and "binance SPOT" in sessions["s3"].error_message
        assert (sessions["s4"].status, sessions["s4"].error_message) == ("error", PRE_P1_STATE)
        assert sessions["s5"].status == "error"
        assert "could not be restored" in sessions["s5"].error_message
        assert not any(s.status == "running" and not s.task_alive for s in sessions.values())
        assert {r["id"]: r["status"] for r in rows} == {
            "s1": "running",
            "s2": "error",
            "s3": "error",
            "s4": "error",
            "s5": "error",
        }

        with pytest.raises(DuplicateSessionError):
            await mgr.start_session("paper-a", "paper")

        ex = s1._executor
        positions = await ex.get_positions("BTCUSDT")
        assert [(p.quantity, p.avg_entry_price) for p in positions] == [(D("0.05"), D("100.1"))]
        ctx = s1._runtime.context.position("BTCUSDT")
        assert ctx is not None and (ctx.direction, ctx.quantity) == (Direction.LONG, D("0.05"))
        assert (await ex.get_balance())["USDT"] == D("1000") - D("5.005") - D("0.005005")
        stops = await ex.fetch_open_orders("BTCUSDT")
        assert [(o["type"], o["amount"], o["stop_price"]) for o in stops] == [
            ("STOP_MARKET", D("0.05"), D("95.09500"))
        ]
        assert mgr.risk is not None and not mgr.risk.recovering
        assert mgr.risk.registered_sessions() == ["s1"]
    finally:
        await h.close()


async def test_junk_files_do_not_block_recovery_of_pre_snapshot_rows(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pre-snapshot row recovers from its file even when unrelated junk sits beside it.

    A list YAML or an unreadable entry in the strategies dir must be skipped, not turn the
    recovery into a _close_restored that sells the restored holdings.
    """
    h = Harness(strategies_dir)
    write_strategy(strategies_dir, "paper-a")
    (strategies_dir / "aaa-list.yaml").write_text("- 1\n- 2\n")
    (strategies_dir / "aaa-unreadable.yaml").mkdir()
    restore_from["s1"] = await _account_with_long(h.end)
    mgr, _ = await _boot(h, [_row("s1", "paper-a")], monkeypatch)
    try:
        s1 = mgr.get_session("s1")
        assert s1 is not None and s1.status == "running" and s1.task_alive
        assert s1.strategy_config is not None and s1.config_hash is not None
        ex = s1._executor
        assert [(p.quantity, p.avg_entry_price) for p in await ex.get_positions("BTCUSDT")] == [
            (D("0.05"), D("100.1"))
        ]
    finally:
        await h.close()


async def test_resting_stop_is_adopted_and_a_gap_during_downtime_fills_at_the_first_bar(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(strategies_dir, history_close=90.0)
    write_strategy(strategies_dir, "paper-a")
    restore_from["s1"] = await _account_with_long(h.end, stop="98")
    mgr, _ = await _boot(h, [_row("s1", "paper-a")], monkeypatch)
    try:
        s1 = mgr.get_session("s1")
        assert s1 is not None and s1.task_alive
        ex = s1._executor
        # the stop restored with the account fired on the first bar after the restart (open 90)
        last = ex.get_fills()[-1]
        assert (last.side, last.reason, last.price) == ("SELL", "stop_loss", D("90.0") * D("0.999"))
        assert await ex.get_positions("BTCUSDT") == []
        assert await ex.fetch_open_orders("BTCUSDT") == []
        assert s1._runtime is not None and s1._runtime.context.position("BTCUSDT") is None
    finally:
        await h.close()


async def test_kill_switch_latched_across_a_restart_flattens_and_does_not_resume(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(strategies_dir)
    write_strategy(strategies_dir, "paper-a")
    restore_from["s1"] = await _account_with_long(h.end)
    store = InMemoryRiskStateStore(paper_capital=D("1000"), kill_switch=True)
    monkeypatch.setattr(sm, "PgRiskStateStore", lambda pool, **kwargs: store)
    app = FastAPI()
    app.state.db_pool = _Pool([_row("s1", "paper-a")])
    await lifespan._init_session_manager(app, HydraConfig(), h.clients())
    mgr: SessionManager = app.state.session_manager
    s1 = mgr.get_session("s1")
    assert s1 is not None
    assert s1.status == "stopped" and not s1.task_alive
    assert "kill switch" in (s1.error_message or "")
    assert await s1._executor.get_positions("BTCUSDT") == []
    assert s1.positions_at_stop == 0
    await mgr.graceful_shutdown()


async def test_display_rows_never_replace_a_running_session(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(strategies_dir)
    write_strategy(strategies_dir, "paper-a")
    restore_from["s1"] = await _account_with_long(h.end)
    stopped = {**_row("s9", "paper-a"), "status": "stopped"}
    mgr, _ = await _boot(h, [_row("s1", "paper-a"), stopped], monkeypatch)
    try:
        s1 = mgr.get_session("s1")
        assert s1 is not None and s1.task_alive and s1._runtime is not None
        assert mgr.get_session("s9") is not None
        assert mgr.get_session("s9").status == "stopped"
        snap = await mgr.health_snapshot()
        assert {(x.session_id, x.desired, x.supervised) for x in snap.sessions} == {
            ("s1", True, True),
            ("s9", False, False),
        }
    finally:
        await h.close()


async def test_restored_account_that_cannot_resume_is_flattened_not_orphaned(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # a REST outage at boot: the account restores, warm-up then finds no history
    h = Harness(strategies_dir, history_bars=0)
    write_strategy(strategies_dir, "paper-a")
    restore_from["s1"] = await _account_with_long(h.end, stop="95")
    rows = [_row("s1", "paper-a")]
    mgr, _ = await _boot(h, rows, monkeypatch)
    try:
        s1 = mgr.get_session("s1")
        assert s1 is not None and s1.status == "error" and not s1.task_alive
        assert "recovery failed" in (s1.error_message or "")
        assert "restored holdings flattened" in (s1.error_message or "")
        ex = s1._executor
        assert await ex.get_positions("BTCUSDT") == []
        assert await ex.fetch_open_orders("BTCUSDT") == []
        last = ex.get_fills()[-1]
        # sold at the restored mark 100 less 0.1 % slippage
        assert (last.side, last.quantity, last.price) == ("SELL", D("0.05"), D("99.9"))
        # 994.989995 + 0.05 x 99.9 - fee 0.004995
        assert (await ex.get_balance())["USDT"] == D("999.980000")
        assert mgr.risk is not None and mgr.risk.registered_sessions() == []
        assert mgr.risk.account().carried_pnl == D("-0.020000")
        assert (s1.positions_at_stop, s1.flatten_incomplete) == (0, False)
        assert rows[0]["status"] == "error"
    finally:
        await h.close()


async def test_unflattened_restored_holdings_stay_reachable_by_the_kill_switch(
    strategies_dir: Path, restore_from: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(strategies_dir, history_bars=0)
    write_strategy(strategies_dir, "paper-a")
    write_strategy(strategies_dir, "paper-b")
    restore_from["s1"] = await _account_with_long(h.end)
    real_flatten = PaperTradingExecutor.flatten
    calls: list[str] = []

    async def first_flatten_fails(self: PaperTradingExecutor, *a: Any, **k: Any) -> Any:
        calls.append(self._session_id)
        if len(calls) == 1:
            raise RuntimeError("no fill")
        return await real_flatten(self, *a, **k)

    monkeypatch.setattr(PaperTradingExecutor, "flatten", first_flatten_fails)
    mgr, _ = await _boot(h, [_row("s1", "paper-a")], monkeypatch)
    try:
        s1 = mgr.get_session("s1")
        assert s1 is not None and s1.status == "error" and s1._runtime is None
        assert (s1.positions_at_stop, s1.flatten_incomplete, s1.holdings) == (1, True, 1)
        assert "1 position(s) could not be flattened" in (s1.error_message or "")
        assert mgr.risk is not None and mgr.risk.registered_sessions() == ["s1"]
        # its 1000 stays allocated while it holds base
        with pytest.raises(ValueError, match="Insufficient paper capital"):
            await mgr.start_session("paper-b", "paper")
        snap = await mgr.health_snapshot()
        assert [(x.session_id, x.open_positions) for x in snap.sessions] == [("s1", 1)]

        report = await mgr.stop_all(actor="test", reason="drill")
        assert (report.sessions_stopped, report.positions_remaining) == (1, 0)
        assert await s1._executor.get_positions("BTCUSDT") == []
        assert (s1.status, s1.positions_at_stop, s1.flatten_incomplete) == ("error", 0, False)
        assert mgr.risk.registered_sessions() == []
    finally:
        await h.close()
