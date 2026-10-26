"""CSRF, CORS and WebSocket Origin checks for browser principals, with no side effect on refusal."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.execution.session_manager import SessionManager, TradingSession
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore
from hydra.services.trader import LocalTrader

from .conftest import TEST_API_TOKEN
from .system_db import FakeSystemDb

DASHBOARD = "https://trading.romulus.hu"
EVIL = "https://evil.romulus.hu"
TRAEFIK = ("10.10.16.2", 41000)
USER = {"Remote-User": "soma"}
CSRF_OK = {"X-Hydra-Request": "1"}

Bind = Callable[..., HydraConfig]


@pytest.fixture()
def default_api_auth() -> None:
    """No default bearer: the browser principal comes from Remote-User via the trusted proxy."""


@pytest.fixture(autouse=True)
def cfg(bind_api_config: Bind) -> HydraConfig:
    return bind_api_config(
        HYDRA_API__AUTH_MODE="proxy",
        HYDRA_API__TRUSTED_PROXY_CIDRS="10.10.16.0/24",
        HYDRA_API__CORS_ORIGINS=DASHBOARD,
        HYDRA_API_TOKEN=TEST_API_TOKEN,
    )


@pytest.fixture()
def session(cfg: HydraConfig, monkeypatch: pytest.MonkeyPatch) -> TradingSession:
    risk = RiskService(
        InMemoryRiskStateStore(paper_capital=Decimal("1000")), market_rules=MarketRulesCache()
    )
    mgr = SessionManager(db_pool=None, config=cfg, risk=risk)
    running = TradingSession(
        session_id="sess-1",
        strategy_id="momentum",
        trading_mode="paper",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
    )
    mgr._sessions[running.session_id] = running
    monkeypatch.setattr(app.state, "session_manager", mgr)
    monkeypatch.setattr(app.state, "trader", LocalTrader(None, mgr))
    return running


@pytest.fixture()
def system_config(monkeypatch: pytest.MonkeyPatch) -> FakeSystemDb:
    db = FakeSystemDb()
    monkeypatch.setattr(app.state, "db_pool", db)
    monkeypatch.setattr(app.state, "system_config", {"default_pair": "BTC/USDT"})
    return db


@pytest.fixture()
def user() -> TestClient:
    return TestClient(app, client=TRAEFIK, headers=USER)


def _assert_csrf_rejected(resp: Any) -> None:
    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert (detail["code"], detail["state"]) == ("csrf_rejected", "forbidden")


REFUSED_HEADERS = [
    pytest.param({}, id="no-header"),
    pytest.param({"X-Hydra-Request": "true"}, id="wrong-header-value"),
    pytest.param({"Content-Type": "text/plain"}, id="simple-form-post"),
    pytest.param({**CSRF_OK, "Origin": EVIL}, id="foreign-origin"),
    pytest.param({**CSRF_OK, "Origin": "null"}, id="null-origin"),
    pytest.param({**CSRF_OK, "Sec-Fetch-Site": "same-site"}, id="same-site"),
    pytest.param(
        {**CSRF_OK, "Origin": DASHBOARD, "Sec-Fetch-Site": "same-site"},
        id="same-site-allowed-origin",
    ),
    pytest.param({**CSRF_OK, "Sec-Fetch-Site": "cross-site"}, id="cross-site"),
]


class TestKillSwitch:
    @pytest.mark.parametrize("headers", REFUSED_HEADERS)
    def test_refused_post_has_no_side_effect(
        self, user: TestClient, session: TradingSession, headers: dict[str, str]
    ) -> None:
        _assert_csrf_rejected(user.post("/api/trading/kill-switch", headers=headers, content="x"))
        assert session.status == "running"

    def test_dashboard_post_stops_the_session(
        self, user: TestClient, session: TradingSession
    ) -> None:
        resp = user.post(
            "/api/trading/kill-switch",
            headers={**CSRF_OK, "Origin": DASHBOARD, "Sec-Fetch-Site": "same-origin"},
        )
        assert resp.status_code == 200
        assert session.status == "stopped"

    def test_header_without_origin_is_enough(
        self, user: TestClient, session: TradingSession
    ) -> None:
        assert user.post("/api/trading/kill-switch", headers=CSRF_OK).status_code == 200
        assert session.status == "stopped"

    def test_service_token_needs_no_csrf_header(self, session: TradingSession) -> None:
        resp = TestClient(app).post(
            "/api/trading/kill-switch", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
        )
        assert resp.status_code == 200
        assert session.status == "stopped"

    def test_dev_mode_still_needs_the_header(
        self, bind_api_config: Bind, session: TradingSession
    ) -> None:
        bind_api_config(HYDRA_API__AUTH_MODE="dev", HYDRA_API__CORS_ORIGINS=DASHBOARD)
        _assert_csrf_rejected(TestClient(app).post("/api/trading/kill-switch"))
        assert session.status == "running"


class TestSystemConfig:
    @pytest.mark.parametrize("headers", REFUSED_HEADERS)
    def test_refused_put_leaves_config_unchanged(
        self, user: TestClient, system_config: FakeSystemDb, headers: dict[str, str]
    ) -> None:
        resp = user.put("/api/system/config", json={"default_pair": "ETH/USDT"}, headers=headers)
        _assert_csrf_rejected(resp)
        assert app.state.system_config == {"default_pair": "BTC/USDT"}
        assert system_config.value("default_pair") == "BTC/USDT"

    def test_dashboard_put_is_applied(self, user: TestClient, system_config: FakeSystemDb) -> None:
        resp = user.put(
            "/api/system/config",
            json={"default_pair": "ETH/USDT"},
            headers={**CSRF_OK, "Origin": DASHBOARD},
        )
        assert resp.status_code == 200
        assert resp.json()["default_pair"] == "ETH/USDT"
        assert app.state.system_config["default_pair"] == "ETH/USDT"
        assert system_config.value("default_pair") == "ETH/USDT"
        assert system_config.config["default_pair"]["updated_by"] == "user:soma"

    @pytest.mark.usefixtures("system_config")
    def test_reads_need_no_csrf_header(self, user: TestClient) -> None:
        assert user.get("/api/system/config").status_code == 200


class TestCors:
    def test_preflight_from_a_foreign_origin_is_not_allowed(self) -> None:
        resp = TestClient(app).options(
            "/api/trading/kill-switch",
            headers={"Origin": EVIL, "Access-Control-Request-Method": "POST"},
        )
        assert "access-control-allow-origin" not in resp.headers

    def test_preflight_from_the_dashboard_allows_the_csrf_header(self) -> None:
        resp = TestClient(app).options(
            "/api/trading/kill-switch",
            headers={
                "Origin": DASHBOARD,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "x-hydra-request, content-type",
            },
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == DASHBOARD
        assert resp.headers["access-control-allow-credentials"] == "true"
        assert "x-hydra-request" in resp.headers["access-control-allow-headers"].lower()

    def test_refusals_carry_cors_headers_so_the_dashboard_can_read_them(self) -> None:
        resp = TestClient(app).get("/api/system/config", headers={"Origin": DASHBOARD})
        assert resp.status_code == 401
        assert resp.headers["access-control-allow-origin"] == DASHBOARD

    def test_origins_follow_the_loaded_config(self, bind_api_config: Bind) -> None:
        bind_api_config(HYDRA_API__AUTH_MODE="dev", HYDRA_API__CORS_ORIGINS=EVIL)
        resp = TestClient(app).get("/api/system/config", headers={"Origin": EVIL})
        assert resp.headers["access-control-allow-origin"] == EVIL


class TestWebSocketOrigin:
    @pytest.mark.parametrize("origin", [EVIL, None])
    def test_browser_upgrade_needs_an_allowed_origin(self, origin: str | None) -> None:
        headers = {**USER, "Origin": origin} if origin else USER
        with (
            pytest.raises(WebSocketDenialResponse) as exc,
            TestClient(app, client=TRAEFIK).websocket_connect("/ws/market", headers=headers),
        ):
            pass
        assert exc.value.status_code == 403
        detail = exc.value.json()["detail"]
        assert (detail["code"], detail["state"]) == ("ws_origin_rejected", "forbidden")

    def test_dashboard_origin_connects(self) -> None:
        headers = {**USER, "Origin": DASHBOARD}
        with TestClient(app, client=TRAEFIK).websocket_connect("/ws/market", headers=headers) as ws:
            ws.send_text("ping")
            assert ws.receive_json() == {"ack": True, "channel": "market"}

    def test_service_token_connects_without_origin(self) -> None:
        headers = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
        with TestClient(app).websocket_connect("/ws/risk", headers=headers) as ws:
            ws.send_text("ping")
            assert ws.receive_json() == {"ack": True, "channel": "risk"}
