"""Trading routes on a real SessionManager: error envelope, kill switch truth, risk-checked
quick-trade with SL/TP placed on the entry fill. Only the ccxt boundary is faked."""

from __future__ import annotations

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from hydra.dashboard.api import app
from hydra.risk.state import RiskStateUnavailableError
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    held,
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

    @pytest.mark.parametrize(
        ("setup", "body", "status", "code", "state"),
        [
            ("ok", {"trading_mode": "live"}, 403, "live_trading_disabled", "not_implemented"),
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
        resp = await api.post("/api/trading/quick-trade", json={"side": "buy", "quantity": "0.5"})
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
        await api.post("/api/trading/quick-trade", json={"side": "buy", "quantity": "0.5"})
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

    async def test_release(self, h: Harness, api: httpx.AsyncClient) -> None:
        await api.post("/api/trading/kill-switch")
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
        resp = await api.post("/api/trading/quick-trade", json={"side": "buy", "quantity": "5"})
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
        ],
    )
    async def test_invalid_requests_are_422(
        self, session: Any, api: httpx.AsyncClient, body: dict[str, Any]
    ) -> None:
        resp = await api.post("/api/trading/quick-trade", json=body)
        assert resp.status_code == 422
        assert _detail(resp)["state"] == "invalid"
        assert executor(session).get_fills() == []

    async def test_sell_without_holdings_is_refused(
        self, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post("/api/trading/quick-trade", json={"side": "sell", "quantity": "0.1"})
        assert (resp.status_code, _detail(resp)["code"]) == (422, "spot_short")

    async def test_market_buy_places_the_stop_on_the_fill(
        self, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={"side": "BUY", "quantity": "0.3", "order_type": "market", "stop_loss": "92"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["status"], body["protection"], body["exit_orders_deferred"]) == (
            "FILLED",
            "armed",
            False,
        )
        orders = await executor(session).fetch_open_orders()
        assert [(o["type"], o["amount"], o["stop_price"], o["id"]) for o in orders] == [
            ("STOP_MARKET", 0.3, 92.0, body["protective_order_id"])
        ]

    async def test_limit_buy_defers_protection_until_the_fill(
        self, h: Harness, session: Any, api: httpx.AsyncClient
    ) -> None:
        resp = await api.post(
            "/api/trading/quick-trade",
            json={
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

    async def test_no_running_session_is_404(self, h: Harness, api: httpx.AsyncClient) -> None:
        resp = await api.post(
            "/api/trading/quick-trade", json={"symbol": "ETHUSDT", "side": "buy", "quantity": "1"}
        )
        assert (resp.status_code, _detail(resp)["code"]) == (404, "session_not_running")


class TestStopAndDetail:
    async def test_stop_reports_positions_at_stop(
        self, h: Harness, api: httpx.AsyncClient, strategies_dir: Path
    ) -> None:
        session = await h.session(write_strategy(strategies_dir, "stop"))
        await h.bar(session, "100")
        await api.post("/api/trading/quick-trade", json={"side": "buy", "quantity": "0.5"})
        resp = await api.delete(f"/api/trading/sessions/{session.session_id}")
        assert resp.status_code == 200
        assert (resp.json()["status"], resp.json()["positions_at_stop"]) == ("stopped", 0)
        detail = (await api.get(f"/api/trading/sessions/{session.session_id}/detail")).json()
        assert detail["metrics"]["total_trades"] == 2
        assert detail["metrics"]["total_pnl"] == pytest.approx(
            float(executor(session).get_account_snapshot().realized_net)
        )

    async def test_unknown_session_is_404(self, h: Harness, api: httpx.AsyncClient) -> None:
        resp = await api.delete("/api/trading/sessions/nope")
        assert (resp.status_code, _detail(resp)["code"]) == (404, "session_not_found")
