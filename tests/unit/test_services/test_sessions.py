"""The session service parity table (MCP-B2): every mapped exception answers the same
envelope the trading route tests pin — status, code, state and extras — so the REST
routes and the MCP tools share one error surface. The dict shapes (created_by, health
fields, quick-trade result) are pinned on the real session seam.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from hydra.core.types import Timeframe
from hydra.data.ingestion import InsufficientHistoryError
from hydra.execution.errors import (
    DuplicateSessionError,
    KillSwitchActiveError,
    LiveTradingDisabledError,
    PaperCapitalUnavailableError,
    SessionNotRunningError,
    SessionPersistError,
    SpotShortError,
    UnsupportedStrategyError,
    UnsupportedVenueError,
)
from hydra.execution.order_manager import RiskRejectedError
from hydra.execution.paper_trading import PaperSessionHaltedError, PersistenceUnavailableError
from hydra.execution.session_manager import KillSwitchReport, TradingSession
from hydra.execution.session_wiring import ManualOrderResult
from hydra.risk.market_rules import MarketRulesUnavailableError
from hydra.services import sessions as sessions_svc
from hydra.services.errors import ServiceError
from hydra.services.sessions import (
    KillSwitchNotPersistedError,
    QuickTradeRequest,
    SessionStopNotPersistedError,
    StartSessionRequest,
)
from tests.unit.test_execution.session_harness import Harness, held, write_strategy

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal
_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _session(**overrides: Any) -> TradingSession:
    fields: dict[str, Any] = {
        "session_id": "s1",
        "strategy_id": "saw",
        "trading_mode": "paper",
        "status": "running",
        "exchange_id": "binance",
        "symbols": ["BTCUSDT"],
        "timeframe": "1h",
        "paper_capital": D("250"),
        "started_at": _NOW,
    }
    fields.update(overrides)
    return TradingSession(**fields)


class _Manager:
    """Configurable stand-in: raises what the manager paths raise, serves one session."""

    def __init__(self) -> None:
        self.start_error: Exception | None = None
        self.stop_error: Exception | None = None
        self.detail_error: Exception | None = None
        self.order_error: Exception | None = None
        self.release_error: Exception | None = None
        self.stop_all_report = KillSwitchReport(
            active=True, persisted=True, sessions_stopped=1, positions_remaining=0
        )
        self.session = _session()
        self.start_actor: str | None = None

    async def start_session(
        self,
        strategy_id: str,
        trading_mode: str,
        paper_capital: Decimal | None = None,
        actor: str | None = None,
    ) -> str:
        self.start_actor = actor
        if self.start_error is not None:
            raise self.start_error
        self.session.created_by = actor
        return self.session.session_id

    async def stop_session(self, session_id: str) -> None:
        if self.stop_error is not None:
            raise self.stop_error

    def list_sessions(self) -> list[TradingSession]:
        return [self.session]

    def get_session(self, session_id: str) -> TradingSession | None:
        return self.session if session_id == self.session.session_id else None

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        if self.detail_error is not None:
            raise self.detail_error
        return {
            "session_id": session_id,
            "strategy_id": self.session.strategy_id,
            "trading_mode": "paper",
            "status": "running",
            "positions": [],
            "trades": [],
        }

    async def submit_manual_order(self, **kwargs: Any) -> tuple[TradingSession, ManualOrderResult]:
        if self.order_error is not None:
            raise self.order_error
        result = ManualOrderResult(
            order_id="o1",
            status="FILLED",
            quantity=D("0.5"),
            price=D("100"),
            protection="armed",
            protective_order_id="p1",
            fill_price=D("100.1"),
            mark_source="last_close",
        )
        return self.session, result

    async def stop_all(self, actor: str, reason: str) -> KillSwitchReport:
        return self.stop_all_report

    async def release_kill_switch(self, actor: str, reason: str) -> None:
        if self.release_error is not None:
            raise self.release_error


def _detail(exc: ServiceError) -> dict[str, Any]:
    detail: dict[str, Any] = exc.detail
    return detail


async def _start(mgr: _Manager, **body: Any) -> dict[str, Any]:
    return await sessions_svc.start_session(
        mgr, "user:soma", StartSessionRequest(strategy_id="saw", **body)
    )


# ---------------------------------------------------------------------------
# Parity table: start
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "status", "code", "state"),
    [
        (KillSwitchActiveError("Kill switch is active"), 409, "kill_switch_active", "invalid"),
        (DuplicateSessionError("already running"), 409, "session_already_running", "invalid"),
        (
            LiveTradingDisabledError("live trading is disabled until P3"),
            403,
            "live_trading_disabled",
            "not_implemented",
        ),
        (UnsupportedVenueError("only binance spot"), 400, "unsupported_venue", "invalid"),
        (UnsupportedStrategyError("bad class"), 400, "strategy_unsupported", "not_implemented"),
        (
            PaperCapitalUnavailableError("no paper capital"),
            503,
            "paper_capital_unavailable",
            "unavailable",
        ),
        (MarketRulesUnavailableError("rules down"), 503, "market_rules_unavailable", "unavailable"),
        (KeyError("ghost"), 404, "strategy_not_found", "not_found"),
        (FileNotFoundError("no dir"), 404, "strategy_not_found", "not_found"),
        (ValueError("Insufficient paper capital"), 400, "invalid_session", "invalid"),
    ],
)
async def test_start_errors_match_the_route_envelope(
    exc: Exception, status: int, code: str, state: str
) -> None:
    """Every mapped start failure: the same status/code/state the route tests pin."""
    mgr = _Manager()
    mgr.start_error = exc
    with pytest.raises(ServiceError) as raised:
        await _start(mgr)
    assert raised.value.status_code == status
    assert (_detail(raised.value)["code"], _detail(raised.value)["state"]) == (code, state)


async def test_start_insufficient_history_keeps_the_extras() -> None:
    mgr = _Manager()
    mgr.start_error = InsufficientHistoryError("binance", "BTCUSDT", Timeframe.H1, 2, 99)
    with pytest.raises(ServiceError) as raised:
        await _start(mgr)
    assert raised.value.status_code == 422
    assert _detail(raised.value) == {
        "code": "insufficient_history",
        "message": str(mgr.start_error),
        "state": "no_data",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "have": 2,
        "need": 99,
    }


async def test_start_refuses_an_unknown_trading_mode() -> None:
    with pytest.raises(ServiceError) as raised:
        await _start(_Manager(), trading_mode="margin")
    assert raised.value.status_code == 400
    assert _detail(raised.value)["code"] == "invalid_trading_mode"


async def test_start_passes_the_actor_and_returns_created_by() -> None:
    mgr = _Manager()
    d = await _start(mgr)
    assert mgr.start_actor == "user:soma"
    assert d["created_by"] == "user:soma"
    assert (d["session_id"], d["status"], d["paper_capital"]) == ("s1", "running", 250.0)


async def test_start_without_a_manager_is_the_503_envelope() -> None:
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.start_session(None, "user:soma", StartSessionRequest(strategy_id="saw"))
    assert raised.value.status_code == 503
    assert _detail(raised.value)["code"] == "engine_not_ready"


# ---------------------------------------------------------------------------
# Parity table: stop, detail, quick trade
# ---------------------------------------------------------------------------


async def test_stop_unknown_session_is_404() -> None:
    mgr = _Manager()
    mgr.stop_error = KeyError("Session nope not found")
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.stop_session(mgr, "nope")
    assert raised.value.status_code == 404
    assert (_detail(raised.value)["code"], _detail(raised.value)["state"]) == (
        "session_not_found",
        "not_found",
    )


async def test_stop_that_cannot_be_persisted_is_the_503_envelope() -> None:
    mgr = _Manager()
    mgr.stop_error = SessionPersistError("db down")
    with pytest.raises(SessionStopNotPersistedError) as raised:
        await sessions_svc.stop_session(mgr, "s1")
    assert isinstance(raised.value, ServiceError)
    assert raised.value.status_code == 503
    assert _detail(raised.value)["code"] == "session_stop_not_persisted"
    assert _detail(raised.value)["state"] == "unavailable"
    assert "still says running" in _detail(raised.value)["message"]


async def test_stop_returns_the_session_dict_with_created_by() -> None:
    d = await sessions_svc.stop_session(_Manager(), "s1")
    assert (d["session_id"], d["created_by"]) == ("s1", None)


async def test_detail_unknown_session_is_404() -> None:
    mgr = _Manager()
    mgr.detail_error = KeyError("Session nope not found")
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.get_session_detail(mgr, "nope")
    assert (raised.value.status_code, _detail(raised.value)["code"]) == (404, "session_not_found")


async def test_detail_carries_created_by_and_strategy_name(
    strategies_dir: Path,
) -> None:
    write_strategy(strategies_dir, "saw")
    detail = await sessions_svc.get_session_detail(_Manager(), "s1")
    assert detail["created_by"] is None
    assert detail["strategy_name"] == "saw"


@pytest.mark.parametrize(
    ("exc", "status", "code", "state"),
    [
        (SessionNotRunningError("no running session"), 404, "session_not_running", "not_found"),
        (SpotShortError("naked short"), 422, "spot_short", "invalid"),
        (ValueError("bad order"), 422, "order_rejected", "invalid"),
        (
            PersistenceUnavailableError("2 unpersisted fills"),
            503,
            "paper_state_unpersisted",
            "unavailable",
        ),
        (PaperSessionHaltedError("ledger broken"), 503, "paper_session_halted", "unavailable"),
    ],
)
async def test_quick_trade_errors_match_the_route_envelope(
    exc: Exception, status: int, code: str, state: str
) -> None:
    mgr = _Manager()
    mgr.order_error = exc
    order = QuickTradeRequest(session_id="s1", side="buy", quantity="0.5")
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.submit_manual_order(mgr, order)
    assert raised.value.status_code == status
    assert (_detail(raised.value)["code"], _detail(raised.value)["state"]) == (code, state)


async def test_quick_trade_risk_rejection_keeps_check_and_intent() -> None:
    mgr = _Manager()
    mgr.order_error = RiskRejectedError("position too large", "position_size", "open")
    order = QuickTradeRequest(session_id="s1", side="buy", quantity="5")
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.submit_manual_order(mgr, order)
    assert raised.value.status_code == 422
    assert _detail(raised.value) == {
        "code": "risk_rejected",
        "message": "position too large",
        "state": "invalid",
        "check": "position_size",
        "intent": "open",
    }


async def test_quick_trade_returns_the_route_response_dict() -> None:
    order = QuickTradeRequest(
        session_id="s1", side="BUY", order_type="market", quantity="0.5", stop_loss="92"
    )
    d = await sessions_svc.submit_manual_order(_Manager(), order)
    assert d == {
        "order_id": "o1",
        "session_id": "s1",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "MARKET",
        "quantity": 0.5,
        "price": None,
        "status": "FILLED",
        "protection": "armed",
        "protective_order_id": "p1",
        "exit_orders_deferred": False,
        "fill_price": 100.1,
        "mark_source": "last_close",
    }


def test_quick_trade_model_is_forbid_extra_and_validates_like_the_route() -> None:
    with pytest.raises(ValidationError):
        QuickTradeRequest(session_id="s1", side="buy", quantity="0.5", trading_mode="paper")
    with pytest.raises(ValidationError):
        QuickTradeRequest(session_id="s1", side="buy", quantity="0.1", order_type="LIMIT")
    with pytest.raises(ValidationError):
        QuickTradeRequest(
            session_id="s1", side="buy", quantity="0.1", stop_loss="110", take_profit="105"
        )
    assert QuickTradeRequest(session_id="s1", side="BUY", quantity="0.5").side == "buy"


# ---------------------------------------------------------------------------
# Parity table: kill switch
# ---------------------------------------------------------------------------


async def test_activate_reports_the_truth() -> None:
    d = await sessions_svc.activate_kill_switch(_Manager(), "user:soma", "drill")
    assert d == {
        "active": True,
        "message": "Kill switch activated - all positions closed and trading halted",
        "sessions_stopped": 1,
        "positions_remaining": 0,
        "persisted": True,
        "flatten_failures": [],
    }


async def test_activate_with_remaining_positions_names_them() -> None:
    mgr = _Manager()
    mgr.stop_all_report = KillSwitchReport(
        active=True,
        persisted=True,
        sessions_stopped=1,
        positions_remaining=2,
        flatten_failures=("s1: 2 position(s) remain",),
    )
    d = await sessions_svc.activate_kill_switch(mgr, "user:soma", "drill")
    assert "could NOT be closed" in d["message"]
    assert "all positions closed" not in d["message"]
    assert d["flatten_failures"] == ["s1: 2 position(s) remain"]


async def test_activate_unpersisted_latch_is_the_503_envelope() -> None:
    mgr = _Manager()
    mgr.stop_all_report = KillSwitchReport(
        active=True, persisted=False, sessions_stopped=1, positions_remaining=0
    )
    with pytest.raises(KillSwitchNotPersistedError) as raised:
        await sessions_svc.activate_kill_switch(mgr, "user:soma", "drill")
    assert raised.value.status_code == 503
    detail = _detail(raised.value)
    assert detail["code"] == "kill_switch_not_persisted"
    assert detail["persisted"] is False
    assert (detail["sessions_stopped"], detail["positions_remaining"]) == (1, 0)
    assert "in memory only" in detail["message"]


async def test_release_without_a_database_is_503_database_unavailable() -> None:
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.release_kill_switch(None, _Manager(), "user:soma", "drill over")
    assert raised.value.status_code == 503
    assert _detail(raised.value)["code"] == "database_unavailable"


async def test_release_that_cannot_be_persisted_is_503_and_names_it() -> None:
    mgr = _Manager()
    mgr.release_error = RuntimeError("db down")
    with pytest.raises(ServiceError) as raised:
        await sessions_svc.release_kill_switch(object(), mgr, "user:soma", "drill over")
    assert raised.value.status_code == 503
    assert _detail(raised.value) == {
        "code": "kill_switch_release_failed",
        "message": "The release could not be persisted; the kill switch stays active",
        "state": "unavailable",
    }


async def test_release_returns_the_released_body() -> None:
    d = await sessions_svc.release_kill_switch(object(), _Manager(), "user:soma", "drill over")
    assert d == {
        "active": False,
        "message": "Kill switch released - trading can resume",
        "sessions_stopped": 0,
    }


# ---------------------------------------------------------------------------
# Dict shapes on the real session seam (the health fields and created_by)
# ---------------------------------------------------------------------------


@pytest.fixture()
async def h(strategies_dir: Path) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    await harness.start()
    yield harness
    await harness.close()


async def test_list_reports_health_fields_and_created_by(h: Harness, strategies_dir: Path) -> None:
    sid = write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    await h.mgr.start_session(sid, "paper", D("250"), actor="service:mcp")
    session = h.mgr.list_sessions()[0]
    await h.bar(session, "100")

    (row,) = sessions_svc.list_sessions(h.mgr)

    assert row["created_by"] == "service:mcp"
    assert row["strategy_name"] == "svc"
    assert row["last_bar_at"] == session.last_bar_at.isoformat()
    assert (row["entries_paused"], row["persist_dirty"], row["feed_live"]) == (None, False, True)


async def test_detail_on_a_real_session_includes_created_by(
    h: Harness, strategies_dir: Path
) -> None:
    sid = write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    s = await h.mgr.start_session(sid, "paper", actor="service:mcp")
    detail = await sessions_svc.get_session_detail(h.mgr, s)
    assert detail["created_by"] == "service:mcp"
    assert detail["metrics"]["balance"]["USDT"] == 1000.0


async def test_quick_trade_round_trip_on_the_real_seam(h: Harness, strategies_dir: Path) -> None:
    sid = write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    session = await h.session(sid)
    await h.bar(session, "100")

    order = QuickTradeRequest(session_id=session.session_id, side="buy", quantity="0.5")
    d = await sessions_svc.submit_manual_order(h.mgr, order)

    assert (d["status"], d["protection"], d["fill_price"]) == ("FILLED", "armed", 100.1)
    assert await held(session) == D("0.5")
