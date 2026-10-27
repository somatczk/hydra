"""The trader's internal server (D5): the bearer gate, one endpoint per op, the envelope.

``build_app`` with a recording backend swapped onto ``app.state.trader``: the routes, the
wire models (reused from ``hydra.services.sessions``), the fail-closed constant-time gate
and the shared error envelope are the real seam; the backend below it is the recorder. No
MCP mount, no CORS, no docs on the internal app.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
from pydantic import SecretStr

from hydra.core.config import HydraConfig
from hydra.services.errors import ServiceError, error_detail
from hydra.services.sessions import QuickTradeRequest, StartSessionRequest
from hydra.trader.server import build_app

TOKEN = "t" * 48

_LIST_OPS = frozenset({"list_sessions", "circuit_breakers", "reset_breaker", "positions"})


class RecordingBackend:
    """Answers canned values and records every op call (name, args, kwargs)."""

    def __init__(self, answers: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.answers = answers or {}
        self.error: ServiceError | None = None

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)

        async def op(*args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, args, kwargs))
            if self.error is not None:
                raise self.error
            if name in self.answers:
                return self.answers[name]
            default: Any = (
                [{"ok": True, "op": name}]
                if name in _LIST_OPS
                else {
                    "ok": True,
                    "op": name,
                }
            )
            return default

        return op

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


def _cfg(token: str | None = TOKEN) -> HydraConfig:
    return HydraConfig(HYDRA_API_TOKEN=SecretStr(token) if token else None)


def _app(backend: RecordingBackend, token: str | None = TOKEN) -> Any:
    app = build_app(_cfg(token), db_pool=None, mgr=None)
    app.state.trader = backend
    return app


def _client(app: Any, *, token: str | None = TOKEN) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://trader", headers=headers
    )


class TestBearerGate:
    async def test_every_op_refuses_an_anonymous_caller(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend), token=None) as client:
            response = await client.get("/ops/sessions")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "unauthenticated"
        assert response.headers["www-authenticate"] == "Bearer"
        assert backend.calls == []

    async def test_a_wrong_token_is_refused(self) -> None:
        backend = RecordingBackend()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(backend)),
            base_url="http://trader",
            headers={"Authorization": "Bearer " + "w" * 48},
        ) as client:
            response = await client.get("/ops/sessions")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "invalid_token"
        assert backend.calls == []

    async def test_the_right_token_reaches_the_op(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend)) as client:
            response = await client.get("/ops/sessions")
        assert response.status_code == 200
        assert backend.names() == ["list_sessions"]

    async def test_no_token_configured_fails_closed(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend, token=None)) as client:
            response = await client.get("/ops/sessions")
        assert response.status_code == 401
        assert response.json()["detail"]["code"] == "auth_not_configured"
        assert backend.calls == []

    async def test_duplicate_authorization_is_a_400(self) -> None:
        backend = RecordingBackend()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app(backend)),
            base_url="http://trader",
            headers=[
                ("Authorization", f"Bearer {TOKEN}"),
                ("Authorization", f"Bearer {TOKEN}"),
            ],
        ) as client:
            response = await client.get("/ops/sessions")
        assert response.status_code == 400
        assert response.json()["detail"]["code"] == "duplicate_header"

    async def test_public_probe_paths_skip_the_gate(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend), token=None) as client:
            assert (await client.get("/livez")).status_code == 503
            assert (await client.get("/readyz")).status_code == 503
            metrics = await client.get("/metrics")
        assert metrics.status_code == 200
        assert metrics.headers["content-type"].startswith("text/plain")
        assert backend.calls == []


class TestEndpoints:
    async def test_start_session_reuses_the_services_request_model(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend)) as client:
            response = await client.post(
                "/ops/start-session",
                json={
                    "actor": "user:ada",
                    "body": {
                        "strategy_id": "sma5",
                        "trading_mode": "paper",
                        "paper_capital": "1000",
                    },
                },
            )
        assert response.status_code == 200, response.json()
        [(name, args, kwargs)] = backend.calls
        assert (name, kwargs) == ("start_session", {})
        assert args[0] == "user:ada"
        assert args[1] == StartSessionRequest(
            strategy_id="sma5", trading_mode="paper", paper_capital="1000"
        )
        assert isinstance(args[1].paper_capital, Decimal)

    async def test_quick_trade_reuses_the_services_request_model(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend)) as client:
            response = await client.post(
                "/ops/quick-trade",
                json={
                    "order": {
                        "session_id": "s1",
                        "symbol": "BTCUSDT",
                        "side": "buy",
                        "order_type": "MARKET",
                        "quantity": "0.5",
                    }
                },
            )
        assert response.status_code == 200, response.json()
        [(name, args, kwargs)] = backend.calls
        assert (name, kwargs) == ("quick_trade", {})
        assert args[0] == QuickTradeRequest(
            session_id="s1", side="buy", order_type="MARKET", quantity="0.5"
        )

    async def test_live_status_threads_the_source_param(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend)) as client:
            response = await client.get("/ops/risk/live-status", params={"source": "live"})
        assert response.status_code == 200
        assert backend.calls == [("live_status", (), {"source": "live"})]

    async def test_webhook_signal_carries_its_kwargs(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend)) as client:
            response = await client.post(
                "/ops/webhook-signal",
                json={
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "action": "entry",
                    "quantity": 0.25,
                    "strategy_id": "sma5",
                },
            )
        assert response.status_code == 200
        assert backend.calls == [
            (
                "webhook_signal",
                (),
                {
                    "symbol": "BTCUSDT",
                    "side": "buy",
                    "action": "entry",
                    "quantity": Decimal("0.25"),
                    "strategy_id": "sma5",
                },
            )
        ]

    async def test_a_service_error_keeps_the_envelope_verbatim(self) -> None:
        backend = RecordingBackend()
        backend.error = ServiceError(
            409,
            error_detail(
                "restart_required", "symbols changed", "invalid", changed_fields=["symbols"]
            ),
        )
        async with _client(_app(backend)) as client:
            response = await client.post("/ops/strategies/sma5/reload")
        assert response.status_code == 409
        assert response.json()["detail"] == {
            "code": "restart_required",
            "message": "symbols changed",
            "state": "invalid",
            "changed_fields": ["symbols"],
        }

    async def test_a_validation_error_uses_the_shared_envelope(self) -> None:
        backend = RecordingBackend()
        async with _client(_app(backend)) as client:
            response = await client.post("/ops/quick-trade", json={"order": {"session_id": "s1"}})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "validation_error"
        assert backend.calls == []


class TestInternalOnlySurface:
    async def test_no_mcp_no_docs_no_cors(self) -> None:
        app = _app(RecordingBackend())
        paths = {getattr(route, "path", "") for route in app.router.routes}
        assert not any(p.startswith("/mcp") for p in paths)
        assert not any(p in ("/docs", "/openapi.json", "/redoc") for p in paths)
        async with _client(app) as client:
            preflight = await client.options(
                "/ops/sessions",
                headers={
                    "Origin": "http://localhost:3000",
                    "Access-Control-Request-Method": "GET",
                },
            )
        assert "access-control-allow-origin" not in preflight.headers
        assert preflight.status_code in (405, 401)
