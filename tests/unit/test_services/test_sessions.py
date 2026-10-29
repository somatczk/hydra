"""The session service parity table (MCP-B2): every mapped exception answers the same
envelope the trading route tests pin — status, code, state and extras — so the REST
routes and the MCP tools share one error surface. The dict shapes (created_by, health
fields, quick-trade result) are pinned on the real session seam.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from hydra.core.config import HydraConfig
from hydra.core.types import OrderType, Timeframe
from hydra.dashboard.routes.trading import SessionDetailResponse
from hydra.data.ingestion import InsufficientHistoryError
from hydra.execution.errors import (
    DuplicateSessionError,
    KillSwitchActiveError,
    LiveCapitalUnavailableError,
    LiveCredentialsError,
    LiveNotArmedError,
    PaperCapitalUnavailableError,
    SessionNotRunningError,
    SessionPersistError,
    SpotShortError,
    UnsupportedStrategyError,
    UnsupportedVenueError,
)
from hydra.execution.live_executor import LiveTradingExecutor
from hydra.execution.order_manager import RiskRejectedError
from hydra.execution.paper_trading import PaperSessionHaltedError, PersistenceUnavailableError
from hydra.execution.protective_orders import ProtectionSpec
from hydra.execution.session_manager import KillSwitchReport, SessionManager, TradingSession
from hydra.execution.session_wiring import ManualOrderResult
from hydra.risk.market_rules import MarketRules, MarketRulesUnavailableError
from hydra.risk.state import RiskStateUnavailableError
from hydra.services import sessions as sessions_svc
from hydra.services.errors import ServiceError
from hydra.services.sessions import (
    KillSwitchNotPersistedError,
    QuickTradeRequest,
    SessionStopNotPersistedError,
    StartSessionRequest,
)
from tests.fixtures.binance_trading import FakeBinanceTradingClient
from tests.unit.test_execution.session_harness import Harness, held, rule, write_strategy

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal
_NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)

_LIVE_RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
    trailing_delta_bips=(D("10"), D("2000")),
)


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
        (LiveNotArmedError("not armed"), 409, "live_not_armed", "invalid"),
        (LiveCredentialsError("no keys"), 403, "live_credentials_missing", "invalid"),
        (LiveCapitalUnavailableError("no live capital"), 403, "live_capital_missing", "invalid"),
        (
            RiskStateUnavailableError("live risk unreadable"),
            503,
            "live_risk_unavailable",
            "unavailable",
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
# The live gate: the armed row is the single master switch (R1)
# ---------------------------------------------------------------------------


class _GateConn:
    def __init__(self, armed: Any) -> None:
        self._armed = armed

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "system_config" in query:
            return None if self._armed is _ABSENT else {"value": self._armed}
        return None


class _GatePool:
    def __init__(self, armed: Any) -> None:
        self._conn = _GateConn(armed)

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[_GateConn]:
        yield self._conn


_ABSENT = object()
_ARMED_ROW = json.dumps({"armed": True, "by": "user:soma", "at": "2026-10-27T00:00:00Z"})


async def test_live_start_refuses_when_there_is_no_pool() -> None:
    """Pool None is disarmed: the live start dies at the gate, never reaching the manager."""
    mgr = _Manager()
    with pytest.raises(ServiceError) as raised:
        await _start(mgr, trading_mode="live")
    assert raised.value.status_code == 409
    assert _detail(raised.value)["code"] == "live_not_armed"
    assert mgr.start_actor is None


async def test_live_start_refuses_when_the_armed_row_is_absent_or_off() -> None:
    for armed in (_ABSENT, json.dumps({"armed": False, "by": "user:soma"}), '{"nonsense"'):
        mgr = _Manager()
        mgr._db_pool = _GatePool(armed)
        with pytest.raises(ServiceError) as raised:
            await _start(mgr, trading_mode="live")
        assert raised.value.status_code == 409
        assert _detail(raised.value)["code"] == "live_not_armed"
        assert mgr.start_actor is None


async def test_live_start_delegates_once_armed() -> None:
    mgr = _Manager()
    mgr._db_pool = _GatePool(_ARMED_ROW)
    d = await _start(mgr, trading_mode="live")
    assert mgr.start_actor == "user:soma"
    assert d["session_id"] == "s1"


# ---------------------------------------------------------------------------
# The perps gate (P4-E): a live FUTURES start answers to the separate ceremony
# ---------------------------------------------------------------------------

PERP_STRATEGY_ID = "perp-momentum"


@pytest.fixture()
def perp_strategy(strategies_dir: Path) -> Path:
    """One saved perps strategy: the venue the deep live-start gate dispatches on."""
    path = strategies_dir / f"{PERP_STRATEGY_ID}.yaml"
    path.write_text(
        "id: perp-momentum\n"
        "name: perp-momentum\n"
        "strategy_class: hydra.strategy.builtin.rule_based.RuleBasedStrategy\n"
        "symbols: [BTCUSDT]\n"
        "exchange:\n"
        "  exchange_id: binanceusdm\n"
        "  market_type: FUTURES\n"
        "  leverage: 2\n"
    )
    return path


class _ConfigConn:
    """Serves the perps/spot system_config rows, keyed (not one row for every key)."""

    def __init__(self, config: dict[str, str]) -> None:
        self._config = config

    async def fetchrow(self, query: str, *args: Any) -> Any:
        assert "system_config" in query, query
        value = self._config.get(args[0])
        return None if value is None else {"value": value}

    async def fetchval(self, query: str, *args: Any) -> Any:
        assert "system_config" in query, query
        return self._config.get(args[0])


class _ConfigPool:
    def __init__(self, config: dict[str, str]) -> None:
        self._conn = _ConfigConn(config)

    @contextlib.asynccontextmanager
    async def acquire(self) -> AsyncIterator[_ConfigConn]:
        yield self._conn


_PERPS_ARMED_ROW = json.dumps({"armed": True, "by": "user:soma", "at": "2026-10-29T00:00:00Z"})


async def _start_strategy(mgr: _Manager, strategy_id: str, **body: Any) -> dict[str, Any]:
    return await sessions_svc.start_session(
        mgr, "user:soma", StartSessionRequest(strategy_id=strategy_id, **body)
    )


async def test_live_futures_start_refuses_while_perps_is_unarmed(perp_strategy: Path) -> None:
    """Even with the spot gate armed, the perps start dies at the separate gate."""
    mgr = _Manager()
    mgr._db_pool = _ConfigPool({"live_armed": _ARMED_ROW, "live_capital": "500.0"})
    with pytest.raises(ServiceError) as raised:
        await _start_strategy(mgr, PERP_STRATEGY_ID, trading_mode="live")
    assert raised.value.status_code == 409
    assert _detail(raised.value)["code"] == "perps_not_armed"
    assert mgr.start_actor is None


async def test_live_futures_start_refuses_armed_without_perps_capital(perp_strategy: Path) -> None:
    mgr = _Manager()
    mgr._db_pool = _ConfigPool({"perps_armed": _PERPS_ARMED_ROW})
    with pytest.raises(ServiceError) as raised:
        await _start_strategy(mgr, PERP_STRATEGY_ID, trading_mode="live")
    assert raised.value.status_code == 403
    assert _detail(raised.value)["code"] == "perps_capital_missing"
    assert mgr.start_actor is None


async def test_live_futures_start_delegates_once_perps_armed(perp_strategy: Path) -> None:
    mgr = _Manager()
    mgr._db_pool = _ConfigPool({"perps_armed": _PERPS_ARMED_ROW, "perps_capital": "500.0"})
    d = await _start_strategy(mgr, PERP_STRATEGY_ID, trading_mode="live")
    assert mgr.start_actor == "user:soma"
    assert d["session_id"] == "s1"


async def test_live_spot_start_ignores_the_perps_rows(perp_strategy: Path) -> None:
    """The perps rows never satisfy the spot gate: the ceremonies are independent."""
    mgr = _Manager()
    mgr._db_pool = _ConfigPool({"perps_armed": _PERPS_ARMED_ROW, "perps_capital": "500.0"})
    with pytest.raises(ServiceError) as raised:
        await _start_strategy(mgr, "saw", trading_mode="live")
    assert _detail(raised.value)["code"] == "live_not_armed"
    assert mgr.start_actor is None


async def test_paper_futures_start_never_consults_the_perps_gate(perp_strategy: Path) -> None:
    """Paper perp sessions run ungated (P4-C's executor): only live mode answers to it."""
    mgr = _Manager()
    d = await _start_strategy(mgr, PERP_STRATEGY_ID)
    assert mgr.start_actor == "user:soma"
    assert d["session_id"] == "s1"


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


# ---------------------------------------------------------------------------
# Webhook signal (the R3 live gate)
# ---------------------------------------------------------------------------


class _RowsConn:
    """Serves the running-session rows the webhook live gate resolves the target from."""

    def __init__(self, rows: list[dict[str, Any]], error: BaseException | None = None) -> None:
        self.rows = rows
        self.error = error

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM trading_sessions" in sql and "running" in sql
        if self.error is not None:
            raise self.error
        return self.rows


class _RowsPool:
    def __init__(self, conn: _RowsConn) -> None:
        self.conn = conn

    def acquire(self) -> _RowsPool:
        return self

    async def __aenter__(self) -> _RowsConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _running_row(mode: str) -> dict[str, Any]:
    return {
        "id": "s-row",
        "strategy_id": "svc",
        "trading_mode": mode,
        "status": "running",
        "symbols": ["BTCUSDT"],
    }


class _OrdersConn:
    """Serves the seeded live_orders rows a stopped session's PgLiveOrderStore reads."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM live_orders" in sql
        return self.rows


class _OrdersPool:
    def __init__(self, conn: _OrdersConn) -> None:
        self.conn = conn

    def acquire(self) -> _OrdersPool:
        return self

    async def __aenter__(self) -> _OrdersConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _stored_live_order_row() -> dict[str, Any]:
    """One asyncpg-shaped live_orders row in the state a stopped session leaves behind."""
    return {
        "client_order_id": "hyd-stopped",
        "session_id": "s1",
        "strategy_id": "saw",
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "side": "BUY",
        "order_type": "LIMIT",
        "quantity": D("0.01"),
        "price": D("90000"),
        "stop_price": None,
        "filled_quantity": D("0.01"),
        "average_fill_price": D("90500"),
        "fee": None,
        "fee_currency": None,
        "reduce_only": False,
        "tag": "entry",
        "actor": "user:soma",
        "status": "filled",
        "exchange_order_id": "101",
        "order_list_id": None,
        "leg_client_order_ids": None,
        "submitted_at": _NOW - timedelta(hours=2),
        "terminal_at": _NOW - timedelta(hours=1),
        "updated_at": _NOW - timedelta(hours=1),
        "error": None,
    }


async def _webhook(h: Harness, pool: _RowsPool) -> dict[str, Any]:
    return await sessions_svc.submit_webhook_order(
        h.mgr,
        symbol="BTCUSDT",
        side="buy",
        action="entry",
        quantity=D("0.01"),
        strategy_id="svc",
        pool=pool,
        webhook_origin=True,
    )


async def test_webhook_refuses_a_live_target_row_before_any_order(
    h: Harness, strategies_dir: Path
) -> None:
    write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    session = await h.session("svc")
    await h.bar(session, "100")

    with pytest.raises(ServiceError) as raised:
        await _webhook(h, _RowsPool(_RowsConn([_running_row("live")])))

    detail = _detail(raised.value)
    assert raised.value.status_code == 409
    assert (detail["code"], detail["state"]) == ("live_gate_required", "invalid")
    assert await held(session) == D("0")


async def test_webhook_trades_a_paper_target_row(h: Harness, strategies_dir: Path) -> None:
    write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    session = await h.session("svc")
    await h.bar(session, "100")

    d = await _webhook(h, _RowsPool(_RowsConn([_running_row("paper")])))

    assert (d["session_id"], d["status"]) == (session.session_id, "FILLED")
    assert await held(session) == D("0.01")


async def test_webhook_refuses_when_the_gate_read_fails(h: Harness, strategies_dir: Path) -> None:
    """R25: the gate fails closed — an unreadable mode refuses the order before it exists."""
    write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    session = await h.session("svc")
    await h.bar(session, "100")

    with pytest.raises(ServiceError) as raised:
        await _webhook(h, _RowsPool(_RowsConn([], error=OSError("database down"))))

    detail = _detail(raised.value)
    assert raised.value.status_code == 503
    assert (detail["code"], detail["state"]) == ("live_gate_unreadable", "unavailable")
    assert await held(session) == D("0")


async def test_webhook_refuses_when_no_pool_is_bound(h: Harness, strategies_dir: Path) -> None:
    """R25's other unreadable mode: no pool at all still never flows past the gate."""
    write_strategy(strategies_dir, "svc")
    assert h.mgr is not None
    session = await h.session("svc")
    await h.bar(session, "100")

    with pytest.raises(ServiceError) as raised:
        await sessions_svc.submit_webhook_order(
            h.mgr,
            symbol="BTCUSDT",
            side="buy",
            action="entry",
            quantity=D("0.01"),
            strategy_id="svc",
            pool=None,
            webhook_origin=True,
        )

    detail = _detail(raised.value)
    assert raised.value.status_code == 503
    assert (detail["code"], detail["state"]) == ("live_gate_unreadable", "unavailable")
    assert await held(session) == D("0")


# ---------------------------------------------------------------------------
# A8-live: the exchange is the truth for open orders and each position's stop
# ---------------------------------------------------------------------------


OCO_LEGS: list[dict[str, Any]] = [
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

OPEN_LISTS: list[dict[str, Any]] = [
    {
        "symbol": "BTCUSDT",
        "orderListId": 5,
        "contingencyType": "OCO",
        "listStatusType": "EXEC_STARTED",
        "listOrderStatus": "EXECUTING",
        "listClientOrderId": "lcid",
        "transactionTime": 1700000000000,
        "orders": [
            {"symbol": "BTCUSDT", "orderId": 11, "clientOrderId": "a"},
            {"symbol": "BTCUSDT", "orderId": 12, "clientOrderId": "b"},
        ],
    }
]


class _FakeKeyedClient:
    """The live executor's keyed ExchangeClient (fetch methods only)."""

    def __init__(
        self,
        legs: list[dict[str, Any]] | None = None,
        lists: list[dict[str, Any]] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.legs = OCO_LEGS if legs is None else legs
        self.lists = OPEN_LISTS if lists is None else lists
        self.error = error

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        return self.legs

    async def fetch_open_order_lists(self, symbol: str | None = None) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        return self.lists


class _FakeProtect:
    """The runtime's protective manager surface the live branch reads."""

    def __init__(self, group: Any) -> None:
        self.group = group

    def armed(self, symbol: str) -> Any:
        return self.group

    def is_protective(self, order_id: str) -> bool:
        return self.group is not None and order_id == self.group.order_id


class _LiveManager:
    """Serves one live session whose executor exposes the keyed client and the group."""

    def __init__(
        self,
        client: _FakeKeyedClient,
        runtime: Any | None = None,
        *,
        status: Any = None,
        store: Any = None,
        trades: list[dict[str, Any]] | None = None,
    ) -> None:
        self.session = _session(trading_mode="live")
        self.session._executor = SimpleNamespace(exchange_client=client, live_orders=store)
        self.session._runtime = runtime
        self.client = client
        self._status = status
        self.trades: list[dict[str, Any]] = list(trades or [])

    def reconcile_status(self, session_id: str) -> Any:
        return self._status

    def list_sessions(self) -> list[TradingSession]:
        return [self.session]

    def get_session(self, session_id: str) -> TradingSession | None:
        return self.session if session_id == self.session.session_id else None

    async def get_session_detail(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "strategy_id": self.session.strategy_id,
            "trading_mode": "live",
            "status": "running",
            "exchange_id": "binance",
            "symbols": ["BTCUSDT"],
            "timeframe": "1h",
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "direction": "long",
                    "quantity": 0.01,
                    "avg_entry_price": 100.0,
                    "unrealized_pnl": 0.0,
                }
            ],
            "trades": list(self.trades),
        }


def _live_group() -> Any:
    from hydra.execution.protective_orders import ProtectiveGroup

    return ProtectiveGroup(
        symbol="BTCUSDT",
        order_id="5",
        order_type=OrderType.OCO,
        quantity=D("0.01"),
        spec=ProtectionSpec(stop_loss=D("98"), take_profit=D("103")),
        leg_ids=("11", "12"),
        confirmed=True,
    )


class TestSessionDetailLive:
    async def test_positions_carry_the_confirmed_exchange_stop(self) -> None:
        mgr = _LiveManager(_FakeKeyedClient(), SimpleNamespace(protect=_FakeProtect(_live_group())))

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        (position,) = detail["positions"]
        stop = position["exchange_stop"]
        assert stop["state"] == "confirmed"
        assert stop["kind"] == "oco"
        assert stop["stop_price"] == 98.0
        assert stop["take_profit"] == 103.0
        assert stop["order_list_id"] == "5"
        assert stop["error"] is None
        assert datetime.fromisoformat(stop["checked_at"]).tzinfo is not None
        # the pinned seam renders through the route model (UI-4 consumes the same names)
        model = SessionDetailResponse.model_validate(detail)
        assert model.positions[0].exchange_stop is not None
        assert model.positions[0].exchange_stop.state == "confirmed"
        assert model.positions[0].exchange_stop.stop_price == 98.0

    async def test_a_held_position_without_a_resting_stop_is_missing(self) -> None:
        mgr = _LiveManager(_FakeKeyedClient(legs=[], lists=[]))

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert detail["positions"][0]["exchange_stop"]["state"] == "missing"

    async def test_an_unreadable_exchange_is_unknown_with_the_error(self) -> None:
        mgr = _LiveManager(_FakeKeyedClient(error=RuntimeError("binance unreachable")))

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        stop = detail["positions"][0]["exchange_stop"]
        assert stop["state"] == "unknown"
        assert "binance unreachable" in (stop["error"] or "")

    async def test_open_orders_come_from_the_normalized_exchange_legs(self) -> None:
        mgr = _LiveManager(_FakeKeyedClient(), SimpleNamespace(protect=_FakeProtect(_live_group())))

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert [
            (o["id"], o["type"], o["stop_price"], o["price"], o["protective"])
            for o in detail["open_orders"]
        ] == [
            ("11", "LIMIT", None, 103.0, True),
            ("12", "STOP_MARKET", 98.0, None, True),
        ]

    async def test_a_paper_session_has_no_exchange_stop(
        self, h: Harness, strategies_dir: Path
    ) -> None:
        sid = write_strategy(
            strategies_dir, "svc-a8", entry_long=rule(("less_than", 100.5), ("greater_than", 99.5))
        )
        session = await h.session(sid)
        await h.bar(session, "100")
        assert await held(session) == D("1.00000")

        detail = await sessions_svc.get_session_detail(h.mgr, session.session_id)

        assert detail["positions"]
        assert all("exchange_stop" not in p for p in detail["positions"])
        model = SessionDetailResponse.model_validate(detail)
        assert all(p.exchange_stop is None for p in model.positions)

    async def test_a_real_live_executor_routes_through_the_live_branch(
        self, strategies_dir: Path
    ) -> None:
        """S1 (R29): a REAL LiveTradingExecutor gets the R12 dispatch — the exchange
        legs as open orders and exchange_stop per position — never the DB-row shape."""
        fake = FakeBinanceTradingClient()
        ex = LiveTradingExecutor(
            fake,
            exchange_id="binance",
            initial_balances={"USDT": D("10000")},
            db_pool=None,
            strategy_id="saw",
            session_id="s1",
            market_rules={"BTCUSDT": _LIVE_RULES},
            poll_interval=5.0,
            resolve_grace=0.0,
        )
        ex.set_mark("BTCUSDT", D("100000"))
        await ex.create_order("BTCUSDT", "BUY", "MARKET", D("0.01"), params={"tag": "entry"})
        await ex.collect_exchange_fills(None)  # the fill reaches the sleeve's ledger
        await ex.create_order(
            "BTCUSDT", "SELL", "OCO", D("0.00999"), price=D("110000"), stop_price=D("95000")
        )
        session = _session(trading_mode="live")
        session._executor = ex
        mgr = SessionManager(config=HydraConfig())
        mgr._sessions["s1"] = session

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert [
            (o["id"], o["type"], o["price"], o["stop_price"], o["oco_id"], o["protective"])
            for o in detail["open_orders"]
        ] == [
            ("101", "LIMIT", 110000.0, None, "1", None),
            ("102", "STOP_MARKET", None, 95000.0, "1", None),
        ]
        (position,) = detail["positions"]
        stop = position["exchange_stop"]
        assert stop["state"] == "confirmed"
        assert stop["kind"] == "oco"
        assert stop["stop_price"] == 95000.0
        assert stop["take_profit"] is None  # no expected group: the stop leg is the proof
        assert stop["order_list_id"] == "1"
        model = SessionDetailResponse.model_validate(detail)
        assert model.positions[0].exchange_stop is not None
        assert model.positions[0].exchange_stop.state == "confirmed"


# ---------------------------------------------------------------------------
# A7 (P3-A2): the tracked orders, the reconcile state and R20's reconciled flag
# ---------------------------------------------------------------------------


async def _seed_store() -> Any:
    from hydra.execution.live_orders import (
        InMemoryLiveOrderStore,
        LiveOrderRecord,
        LiveOrderStatus,
    )

    store = InMemoryLiveOrderStore()
    for i, (cid, status, submitted) in enumerate(
        [
            ("hyd-old", LiveOrderStatus.FILLED, _NOW - timedelta(hours=2)),
            ("hyd-new", LiveOrderStatus.NEW, _NOW - timedelta(hours=1)),
            ("hyd-newest", LiveOrderStatus.PARTIALLY_FILLED, _NOW),
        ]
    ):
        await store.create(
            LiveOrderRecord(
                client_order_id=cid,
                session_id="s1",
                strategy_id="saw",
                exchange_id="binance",
                symbol="BTCUSDT",
                side="BUY",
                order_type="LIMIT",
                quantity=D("0.01"),
                price=D("90000"),
                stop_price=D("88000") if i == 2 else None,
                filled_quantity=D("0.004") if i == 2 else D("0.01") if i == 0 else D("0"),
                average_fill_price=D("90500") if i == 0 else None,
                status=status,
                tag="entry",
                actor="user:someone",
                exchange_order_id=f"10{i}",
                submitted_at=submitted,
                terminal_at=_NOW if i == 0 else None,
                error="stop moved" if i == 2 else None,
            )
        )
    return store


class TestSessionDetailA7:
    async def test_orders_are_the_tracked_rows_newest_first_in_the_pinned_shape(self) -> None:
        from hydra.execution.reconcile import NEVER_STATUS

        mgr = _LiveManager(_FakeKeyedClient(), status=NEVER_STATUS, store=await _seed_store())

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert [o["client_order_id"] for o in detail["orders"]] == [
            "hyd-newest",
            "hyd-new",
            "hyd-old",
        ]
        newest = detail["orders"][0]
        assert set(newest) == {
            "client_order_id",
            "exchange_order_id",
            "symbol",
            "side",
            "order_type",
            "quantity",
            "price",
            "stop_price",
            "filled_quantity",
            "average_fill_price",
            "status",
            "tag",
            "actor",
            "submitted_at",
            "terminal_at",
            "error",
        }
        assert newest["status"] == "partially_filled"
        assert newest["error"] == "stop moved"

    async def test_orders_come_from_the_live_orders_table_without_an_executor(self) -> None:
        """A stopped live session (display-only row) still reads its tracked orders
        through PgLiveOrderStore over the manager's pool (S6: the claim, proven)."""
        from hydra.execution.reconcile import NEVER_STATUS

        mgr = _LiveManager(_FakeKeyedClient(), status=NEVER_STATUS)
        mgr.session._executor = None
        mgr._db_pool = _OrdersPool(_OrdersConn([_stored_live_order_row()]))

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert [o["client_order_id"] for o in detail["orders"]] == ["hyd-stopped"]
        assert detail["orders"][0]["status"] == "filled"
        assert detail["orders"][0]["quantity"] == 0.01
        assert detail["orders"][0]["submitted_at"] == (_NOW - timedelta(hours=2)).isoformat()

    async def test_without_a_pool_a_stopped_live_session_has_no_orders(self) -> None:
        """The degenerate leg: no executor, no pool — nothing to read, honestly empty."""
        from hydra.execution.reconcile import NEVER_STATUS

        mgr = _LiveManager(_FakeKeyedClient(), status=NEVER_STATUS)
        mgr.session._executor = None
        mgr._db_pool = None

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert detail["orders"] == []

    async def test_reconcile_is_served_verbatim(self) -> None:
        from hydra.execution.reconcile import ReconcileStatus

        at = _NOW - timedelta(minutes=5)
        status = ReconcileStatus(
            state="mismatch", last_reconciled_at=at, detail="untracked order 7 on BTCUSDT"
        )
        mgr = _LiveManager(_FakeKeyedClient(), status=status)

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert detail["reconcile"] == {
            "state": "mismatch",
            "last_reconciled_at": at.isoformat(),
            "detail": "untracked order 7 on BTCUSDT",
        }
        model = SessionDetailResponse.model_validate(detail)
        assert model.reconcile is not None
        assert model.reconcile.state == "mismatch"
        assert model.reconcile.detail == "untracked order 7 on BTCUSDT"

    async def test_trades_carry_the_r20_reconciled_truth_table(self) -> None:
        from hydra.execution.reconcile import ReconcileStatus

        covered = _NOW - timedelta(minutes=10)
        status = ReconcileStatus(state="ok", last_reconciled_at=covered, covered_at=covered)
        mgr = _LiveManager(_FakeKeyedClient(), status=status)
        mgr.trades = [
            {"id": "t1", "timestamp": (_NOW - timedelta(hours=1)).isoformat()},
            {"id": "t2", "timestamp": (_NOW).isoformat()},
        ]

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert [t["reconciled"] for t in detail["trades"]] == [True, False]

    async def test_fills_before_the_first_covering_pass_are_not_reconciled(self) -> None:
        from hydra.execution.reconcile import ReconcileStatus

        mgr = _LiveManager(_FakeKeyedClient(), status=ReconcileStatus(state="never"))
        mgr.trades = [{"id": "t1", "timestamp": _NOW.isoformat()}]

        detail = await sessions_svc.get_session_detail(mgr, "s1")

        assert detail["trades"][0]["reconciled"] is False

    async def test_the_list_and_session_dict_carry_reconcile_state(self) -> None:
        from hydra.execution.reconcile import NEVER_STATUS

        mgr = _LiveManager(_FakeKeyedClient(), status=NEVER_STATUS)
        rows = sessions_svc.list_sessions(mgr)
        assert [r["reconcile_state"] for r in rows] == ["never"]

        d = sessions_svc.session_to_dict(
            mgr.session, reconcile_state=sessions_svc._reconcile_state(mgr, mgr.session)
        )
        assert d["reconcile_state"] == "never"
        paper = sessions_svc.session_to_dict(_session())
        assert paper["reconcile_state"] is None
