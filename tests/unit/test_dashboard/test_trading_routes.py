"""Trading routes on a real SessionManager: error envelope, kill switch truth, risk-checked
quick-trade with SL/TP placed on the entry fill. Only the ccxt boundary is faked."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from hydra.dashboard.api import app
from hydra.execution.paper_trading import PaperSessionHaltedError, PersistenceUnavailableError
from hydra.execution.session_manager import TradingSession
from hydra.risk.state import RiskStateUnavailableError
from hydra.services.trader import LocalTrader
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    held,
    settle,
    write_strategy,
)

from .conftest import TEST_API_TOKEN

pytestmark = pytest.mark.filterwarnings("error::RuntimeWarning")

D = Decimal


@pytest.fixture()
async def h(strategies_dir: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Harness]:
    harness = Harness(strategies_dir)
    mgr = await harness.start()
    monkeypatch.setattr(app.state, "session_manager", mgr)
    monkeypatch.setattr(app.state, "trader", LocalTrader(app.state.db_pool, mgr))
    yield harness
    await harness.close()


@pytest.fixture()
async def api() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://engine",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    ) as client:
        yield client


@pytest.fixture()
def broadcasts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    from hydra.core.websocket import manager

    sent: list[dict[str, Any]] = []

    async def broadcast(channel: str, payload: dict[str, Any]) -> None:
        sent.append(payload)

    monkeypatch.setattr(manager, "broadcast", broadcast)
    return sent


class _DownPool:
    def acquire(self) -> Any:
        raise ConnectionError("database down")


class _HangingPool:
    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[Any]:
        await asyncio.sleep(60)
        yield None


class _TradesPool:
    """Answers the trades query with only the columns its SELECT names."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    @asynccontextmanager
    async def acquire(self) -> AsyncIterator[_TradesPool]:
        yield self

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        if "FROM trades" not in query:
            return []
        cols = [c.strip() for c in query.split("SELECT")[1].split("FROM")[0].split(",")]
        return [{c: r[c] for c in cols} for r in self.rows]


def _detail(resp: httpx.Response) -> dict[str, Any]:
    detail: dict[str, Any] = resp.json()["detail"]
    return detail


async def _start(api: httpx.AsyncClient, strategy_id: str, **body: Any) -> httpx.Response:
    return await api.post(
        "/api/trading/sessions",
        json={"strategy_id": strategy_id, "trading_mode": "paper", **body},
    )


class TestStartSession:
    async def test_paper_session_starts(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        write_strategy(strategies_dir, "ok")
        resp = await _start(api, "ok", paper_capital=250)
        assert resp.status_code == 201, resp.text
        assert (resp.json()["status"], resp.json()["paper_capital"]) == ("running", 250.0)
        assert resp.json()["created_by"] == "service:api-token"

    @pytest.mark.parametrize(
        ("setup", "body", "status", "code", "state"),
        [
            ("ok", {"trading_mode": "live"}, 409, "live_not_armed", "invalid"),
            ("bybit", {}, 400, "unsupported_venue", "invalid"),
            ("futures", {}, 400, "unsupported_venue", "invalid"),
            ("dca", {}, 400, "strategy_unsupported", "not_implemented"),
            ("missing", {}, 404, "strategy_not_found", "not_found"),
            ("history", {}, 422, "insufficient_history", "no_data"),
            ("ok", {"trading_mode": "margin"}, 400, "invalid_trading_mode", "invalid"),
        ],
    )
    async def test_refusals_use_the_envelope(
        self,
        h: Harness,
        api: httpx.AsyncClient,
        strategies_dir: Path,
        setup: str,
        body: dict[str, Any],
        status: int,
        code: str,
        state: str,
    ) -> None:
        if setup == "ok":
            write_strategy(strategies_dir, "ok")
        elif setup == "bybit":
            write_strategy(strategies_dir, "bybit", exchange_id="bybit")
        elif setup == "futures":
            write_strategy(strategies_dir, "futures", market_type="FUTURES")
        elif setup == "dca":
            write_strategy(
                strategies_dir, "dca", strategy_class="hydra.strategy.builtin.dca.DCAStrategy"
            )
        elif setup == "history":
            path = strategies_dir / f"{write_strategy(strategies_dir, 'history')}.yaml"
            path.write_text(path.read_text().replace("required_history: 2", "required_history: 99"))
        resp = await _start(api, setup, **body)
        assert resp.status_code == status, resp.text
        assert (_detail(resp)["code"], _detail(resp)["state"]) == (code, state)
        assert h.mgr is not None and h.mgr.list_sessions() == []

    async def test_kill_switch_and_duplicate_are_conflicts(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        write_strategy(strategies_dir, "dup")
        assert (await _start(api, "dup", paper_capital=100)).status_code == 201
        resp = await _start(api, "dup", paper_capital=100)
        assert (resp.status_code, _detail(resp)["code"]) == (409, "session_already_running")
        assert h.mgr is not None and h.mgr.risk is not None
        await h.mgr.risk.activate_kill_switch("test", "drill")
        write_strategy(strategies_dir, "other")
        resp = await _start(api, "other")
        assert (resp.status_code, _detail(resp)["code"]) == (409, "kill_switch_active")


class TestKillSwitch:
    async def test_activate_flattens_and_reports_the_truth(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "ks"))
        await h.bar(session, "100")
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "buy", "quantity": "0.5"},
        )
        assert resp.status_code == 200, resp.text
        resp = await api.post("/api/trading/kill-switch")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["active"], body["sessions_stopped"], body["positions_remaining"]) == (
            True,
            1,
            0,
        )
        assert body["persisted"] is True and "all positions closed" in body["message"]
        assert await held(session) == 0 and session.status == "stopped"

    async def test_remaining_positions_are_not_reported_as_halted(
        self,
        h: Harness,
        api: httpx.AsyncClient,
        strategies_dir: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "stuck"))
        await h.bar(session, "100")
        await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "buy", "quantity": "0.5"},
        )
        ex = executor(session)
        real = ex.create_order

        async def no_market_sell(*args: Any, **kwargs: Any) -> Any:
            if (kwargs.get("side"), kwargs.get("order_type")) == ("SELL", "MARKET"):
                raise RuntimeError("exchange down")
            return await real(*args, **kwargs)

        async def broken(*_a: Any, **_k: Any) -> Any:
            raise RuntimeError("exchange down")

        monkeypatch.setattr(ex, "create_order", no_market_sell)
        monkeypatch.setattr(ex, "flatten", broken)
        body = (await api.post("/api/trading/kill-switch")).json()
        assert body["positions_remaining"] == 1
        assert "could NOT be closed" in body["message"]
        assert "all positions closed" not in body["message"]

    async def test_unpersisted_latch_is_a_503(
        self,
        h: Harness,
        api: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def db_down(active: bool) -> None:
            raise RiskStateUnavailableError("db down")

        assert h.store is not None
        monkeypatch.setattr(h.store, "set_kill_switch", db_down)
        resp = await api.post("/api/trading/kill-switch")
        assert resp.status_code == 503
        assert (_detail(resp)["code"], _detail(resp)["persisted"]) == (
            "kill_switch_not_persisted",
            False,
        )
        assert h.mgr is not None and h.mgr.kill_switch_active()

    @pytest.mark.parametrize(
        "body",
        [None, {"confirm": "yes", "reason": "drill over"}, {"confirm": "RELEASE", "reason": "x"}],
    )
    async def test_release_needs_confirmation_and_a_reason(
        self, h: Harness, api: httpx.AsyncClient, body: dict[str, Any] | None
    ) -> None:
        await api.post("/api/trading/kill-switch")
        resp = await api.request("DELETE", "/api/trading/kill-switch", json=body)
        assert resp.status_code == 422
        assert h.mgr is not None and h.mgr.kill_switch_active()

    async def test_release_without_a_database_is_503_and_stays_latched(
        self, h: Harness, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await api.post("/api/trading/kill-switch")
        monkeypatch.setattr(app.state.trader, "db_pool", None)
        resp = await api.request(
            "DELETE",
            "/api/trading/kill-switch",
            json={"confirm": "RELEASE", "reason": "drill over"},
        )
        assert (resp.status_code, _detail(resp)["code"]) == (503, "database_unavailable")
        assert h.mgr is not None and h.mgr.kill_switch_active()

    async def test_release(
        self, h: Harness, api: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await api.post("/api/trading/kill-switch")
        monkeypatch.setattr(app.state.trader, "db_pool", object())
        resp = await api.request(
            "DELETE",
            "/api/trading/kill-switch",
            json={"confirm": "RELEASE", "reason": "drill over"},
        )
        assert resp.status_code == 200
        assert h.mgr is not None and not h.mgr.kill_switch_active()


class TestQuickTrade:
    @pytest.fixture()
    async def session(self, h: Harness, strategies_dir: Path) -> Any:
        session = await h.session(write_strategy(strategies_dir, "qt"))
        await h.bar(session, "100")
        return session

    async def test_oversized_order_is_risk_rejected(
        self, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "buy", "quantity": "5"},
        )
        assert resp.status_code == 422
        assert (_detail(resp)["code"], _detail(resp)["check"]) == ("risk_rejected", "position_size")
        assert executor(session).get_fills() == []

    @pytest.mark.parametrize(
        "body",
        [
            {"side": "buy", "quantity": "-1"},
            {"side": "buy", "quantity": "0"},
            {"side": "hold", "quantity": "1"},
            {"side": "buy", "quantity": "0.1", "order_type": "LIMIT"},
            {"side": "buy", "quantity": "0.1", "stop_loss": "90", "trailing_stop_pct": "2"},
            {"side": "buy", "quantity": "0.1", "stop_loss": "110", "take_profit": "105"},
            {"side": "buy", "quantity": "0.1", "session_id": ""},
        ],
    )
    async def test_invalid_requests_are_422(
        self, session: Any, api: httpx.AsyncClient, body: dict[str, Any]
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade", json={"session_id": session.session_id, **body}
        )
        assert resp.status_code == 422
        assert _detail(resp)["state"] == "invalid"
        assert executor(session).get_fills() == []

    async def test_sell_without_holdings_is_refused(
        self, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "sell", "quantity": "0.1"},
        )
        assert (resp.status_code, _detail(resp)["code"]) == (422, "spot_short")

    async def test_market_buy_places_the_stop_on_the_fill(
        self, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "BUY",
                "quantity": "0.3",
                "order_type": "market",
                "stop_loss": "92",
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["status"], body["protection"], body["exit_orders_deferred"]) == (
            "FILLED",
            "armed",
            False,
        )
        assert (body["fill_price"], body["mark_source"]) == (100.1, "last_close")
        orders = await executor(session).fetch_open_orders()
        assert [(o["type"], o["amount"], o["stop_price"], o["id"]) for o in orders] == [
            ("STOP_MARKET", 0.3, 92.0, body["protective_order_id"])
        ]

    async def test_market_fill_reports_the_live_price_it_used(
        self, h: Harness, session: Any, api: httpx.AsyncClient
    ) -> None:
        await h.price(session, "103")

        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "buy", "quantity": "0.3"},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["status"], body["price"], body["fill_price"], body["mark_source"]) == (
            "FILLED",
            None,
            103.103,
            "live",
        )

    async def test_limit_buy_defers_protection_until_the_fill(
        self, h: Harness, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "buy",
                "quantity": "0.2",
                "order_type": "limit",
                "price": "98",
                "stop_loss": "90",
                "take_profit": "110",
            },
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["exit_orders_deferred"] is True
        assert [o["type"] for o in await executor(session).fetch_open_orders()] == ["LIMIT"]

        await h.bar(session, "99", open="100", high="100", low="97")
        fills = executor(session).get_fills()
        assert [(f.side, f.price, f.liquidity) for f in fills] == [("BUY", D("98"), "maker")]
        legs = sorted(
            (o["type"], o["amount"], o["stop_price"], o["price"])
            for o in await executor(session).fetch_open_orders()
        )
        assert legs == [("LIMIT", 0.2, None, 110.0), ("STOP_MARKET", 0.2, 90.0, None)]

    async def test_limit_buy_outlives_the_stale_cleanup_until_a_bar_fills_it(
        self, h: Harness, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "buy",
                "quantity": "0.2",
                "order_type": "LIMIT",
                "price": "98",
                "stop_loss": "90",
            },
        )
        assert resp.status_code == 200, resp.text
        om = session._order_manager
        # two primary (1h) bars, not the 300 s live default
        assert om._stale_order_timeout == 7200.0
        assert await om.cleanup_stale_orders() == []
        await h.bar(session, "99", open="100", high="100", low="97")
        assert [(f.side, f.price) for f in executor(session).get_fills()] == [("BUY", D("98"))]
        orders = await executor(session).fetch_open_orders()
        assert [(o["type"], o["stop_price"]) for o in orders] == [("STOP_MARKET", 90.0)]

    async def test_stale_limit_entry_releases_its_protection_and_tells_the_client(
        self,
        h: Harness,
        session: Any,
        api: httpx.AsyncClient,
        broadcasts: list[dict[str, Any]],
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "buy",
                "quantity": "0.2",
                "order_type": "LIMIT",
                "price": "98",
                "stop_loss": "90",
            },
        )
        order_id = resp.json()["order_id"]
        runtime = session._runtime
        assert len(runtime.protect._manual) == 1
        session._order_manager._stale_order_timeout = 0.0
        assert await session._order_manager.cleanup_stale_orders() == [order_id]
        await settle()
        assert runtime.protect._manual == {}
        cancels = [b for b in broadcasts if b.get("event") == "order_cancel"]
        assert [(b["order_id"], b["reason"]) for b in cancels] == [(order_id, "user_cancel")]
        await h.bar(session, "99", open="100", high="100", low="97")
        assert executor(session).get_fills() == []
        assert await executor(session).fetch_open_orders() == []

    @pytest.mark.parametrize(
        ("protection", "status", "stop"),
        [
            # 0.9 x (100 - 70) = 27 > 2 % of the 1000 sleeve
            ({"stop_loss": "70"}, 422, None),
            # trailing 25 %: initial stop 75, 0.9 x 25 = 22.5 > 20
            ({"trailing_stop_pct": "25"}, 422, None),
            # the strategy's 5 % default from the limit price: 0.9 x 5 = 4.5
            ({}, 200, 95.0),
        ],
    )
    async def test_limit_entry_is_risk_checked_against_its_stop(
        self,
        session: Any,
        api: httpx.AsyncClient,
        protection: dict[str, str],
        status: int,
        stop: float | None,
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "buy",
                "quantity": "0.9",
                "order_type": "LIMIT",
                "price": "100",
                **protection,
            },
        )
        assert resp.status_code == status, resp.text
        orders = await executor(session).fetch_open_orders()
        if status == 422:
            assert (_detail(resp)["code"], _detail(resp)["check"]) == (
                "risk_rejected",
                "risk_per_trade",
            )
            assert orders == []
        else:
            assert [(o["type"], o["price"], o["stop_price"]) for o in orders] == [
                ("LIMIT", 100.0, stop)
            ]

    @pytest.mark.parametrize(
        ("error", "code"),
        [
            (PersistenceUnavailableError("2 unpersisted fills"), "paper_state_unpersisted"),
            (PaperSessionHaltedError("ledger invariant broken"), "paper_session_halted"),
        ],
    )
    async def test_unavailable_paper_account_is_503(
        self,
        session: Any,
        api: httpx.AsyncClient,
        monkeypatch: pytest.MonkeyPatch,
        error: Exception,
        code: str,
    ) -> None:
        async def refuse(*_a: Any, **_k: Any) -> Any:
            raise error

        monkeypatch.setattr(executor(session), "create_order", refuse)
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "buy", "quantity": "0.1"},
        )
        assert (resp.status_code, _detail(resp)["code"]) == (503, code)
        assert _detail(resp)["state"] == "unavailable"

    async def test_no_running_session_is_404(self, h: Harness, api: httpx.AsyncClient) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": "nope", "symbol": "BTCUSDT", "side": "buy", "quantity": "1"},
        )
        assert (resp.status_code, _detail(resp)["code"]) == (404, "session_not_running")

    async def test_session_id_is_required_and_picks_the_session(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "qt1"), capital="500")
        other = await h.session(write_strategy(strategies_dir, "qt2"), capital="400")
        await h.bar(session, "100")
        await h.bar(other, "100")
        resp = await api.post("/api/trading/quick-trade", json={"side": "buy", "quantity": "0.1"})
        assert resp.status_code == 422
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"session_id": other.session_id, "side": "buy", "quantity": "0.1"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_id"] == other.session_id
        assert (await held(other), await held(session)) == (D("0.1"), D("0"))


class TestStopAndDetail:
    async def test_stop_reports_positions_at_stop(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "stop"))
        await h.bar(session, "100")
        await api.post(
            "/api/trading/quick-trade",
            json={"session_id": session.session_id, "side": "buy", "quantity": "0.5"},
        )
        resp = await api.delete(f"/api/trading/sessions/{session.session_id}")
        assert resp.status_code == 200
        assert (resp.json()["status"], resp.json()["positions_at_stop"]) == ("stopped", 0)
        detail = (await api.get(f"/api/trading/sessions/{session.session_id}/detail")).json()
        assert detail["metrics"]["total_trades"] == 2
        assert [(t["side"], t["quantity"], t["reason"]) for t in detail["trades"]] == [
            ("BUY", 0.5, "market"),
            ("SELL", 0.5, "market"),
        ]
        assert detail["open_orders"] == []
        assert detail["created_by"] is None  # started by the harness, not through an actor
        assert detail["metrics"]["total_pnl"] == pytest.approx(
            float(executor(session).get_account_snapshot().realized_net)
        )

    async def test_stop_that_cannot_be_persisted_is_503(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "unsaved"))
        await h.bar(session, "100")
        assert h.mgr is not None
        h.mgr._db_pool = _DownPool()
        resp = await api.delete(f"/api/trading/sessions/{session.session_id}")
        assert (resp.status_code, _detail(resp)["code"]) == (503, "session_stop_not_persisted")
        assert session.status == "stopped" and not session.task_alive
        listed = (await api.get("/api/trading/sessions")).json()
        assert [s["persist_error"] for s in listed] == [
            "session status 'stopped' not persisted; retrying every minute"
        ]
        h.mgr._db_pool = None

    async def test_unknown_session_is_404(self, h: Harness, api: httpx.AsyncClient) -> None:
        resp = await api.delete("/api/trading/sessions/nope")
        assert (resp.status_code, _detail(resp)["code"]) == (404, "session_not_found")

    async def test_detail_lists_resting_orders_and_which_are_protective(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "orders"))
        await h.bar(session, "100")
        await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "buy",
                "quantity": "0.3",
                "stop_loss": "92",
                "take_profit": "110",
            },
        )
        await api.post(
            "/api/trading/quick-trade",
            json={
                "session_id": session.session_id,
                "side": "buy",
                "quantity": "0.1",
                "order_type": "limit",
                "price": "95",
            },
        )
        detail = (await api.get(f"/api/trading/sessions/{session.session_id}/detail")).json()
        assert [
            tuple(
                o[k]
                for k in ("side", "type", "amount", "price", "stop_price", "reason", "protective")
            )
            for o in detail["open_orders"]
        ] == [
            ("SELL", "LIMIT", 0.3, 110.0, None, "take_profit", True),
            ("SELL", "STOP_MARKET", 0.3, None, 92.0, "stop_loss", True),
            ("BUY", "LIMIT", 0.1, 95.0, None, "limit", False),
        ]


class TestSessionListHealth:
    async def test_list_reports_bar_feed_and_ledger_health(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "health"))
        await h.bar(session, "100")
        assert session.last_bar_at is not None
        (row,) = (await api.get("/api/trading/sessions")).json()
        assert (row["last_bar_at"], row["entries_paused"], row["persist_dirty"]) == (
            session.last_bar_at.isoformat(),
            None,
            False,
        )
        assert row["feed_live"] is True

        await api.delete(f"/api/trading/sessions/{session.session_id}")
        (row,) = (await api.get("/api/trading/sessions")).json()
        assert (row["status"], row["feed_live"], row["entries_paused"]) == (
            "stopped",
            False,
            None,
        )

    async def test_list_never_waits_on_the_db_and_shows_paused_dirty_and_ledgerless(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "busy"))
        await h.bar(session, "100")
        assert session._runtime is not None
        session._runtime.entries_paused = "feed stale: binance"
        executor(session)._set_dirty(True)
        h.mgr._sessions["restored"] = TradingSession("restored", "old", "paper", status="stopped")
        h.mgr._db_pool = _HangingPool()

        resp = await asyncio.wait_for(api.get("/api/trading/sessions"), 1)
        h.mgr._db_pool = None

        rows = {r["session_id"]: r for r in resp.json()}
        assert [
            rows[session.session_id][k]
            for k in ("last_bar_at", "entries_paused", "persist_dirty", "feed_live")
        ] == [session.last_bar_at.isoformat(), "feed stale: binance", True, True]
        assert [
            rows["restored"][k]
            for k in ("last_bar_at", "entries_paused", "persist_dirty", "feed_live")
        ] == [None, None, None, False]

    async def test_restored_session_fills_carry_quantity_and_reason(
        self, h: Harness, api: httpx.AsyncClient
    ) -> None:
        h.mgr._sessions["restored"] = TradingSession("restored", "old", "paper", status="stopped")
        h.mgr._db_pool = _TradesPool(
            [
                {
                    "id": 7,
                    "symbol": "BTCUSDT",
                    "side": "SELL",
                    "quantity": D("0.5"),
                    "price": D("90"),
                    "fee": D("0.045"),
                    "pnl": D("-5"),
                    "net_pnl": D("-5.095"),
                    "reason": "stop_loss",
                    "order_type": "STOP_MARKET",
                    "timestamp": datetime(2026, 10, 1, tzinfo=UTC),
                }
            ]
        )

        detail = (await api.get("/api/trading/sessions/restored/detail")).json()

        assert [(t["side"], t["quantity"], t["price"], t["reason"]) for t in detail["trades"]] == [
            ("SELL", 0.5, 90.0, "stop_loss")
        ]
