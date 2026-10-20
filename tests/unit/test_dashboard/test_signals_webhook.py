"""Signal webhook: fail-closed secret, constant-time compare, one strategy's session only.

Sessions are real paper sessions (SessionManager, OrderManager, RiskService, PaperTradingExecutor);
only the ccxt boundary is faked by the session harness. Orders go through submit_manual_order.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from hydra.dashboard.api import app
from hydra.dashboard.routes import signals
from hydra.execution.paper_trading import PaperTradingExecutor
from tests.unit.test_execution.session_harness import Harness, executor, write_strategy

from .conftest import TEST_API_TOKEN

SECRET = secrets.token_hex(16)
START = Decimal("500")


@pytest.fixture()
async def sessions(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[dict[str, PaperTradingExecutor]]:
    h = Harness(strategies_dir)
    mgr = await h.start()
    made = {}
    for sid in ("momentum", "breakout"):
        write_strategy(strategies_dir, sid)
        made[sid] = executor(await h.session(sid, str(START)))
    monkeypatch.setattr(app.state, "session_manager", mgr)
    yield made
    await h.close()


def _secret(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    cfg = {} if value is None else {"webhook_secret": value}
    monkeypatch.setattr(app.state, "system_config", cfg)


MISSING = object()


async def _post(**body: Any) -> httpx.Response:
    payload = {"symbol": "BTCUSDT", "side": "buy", "strategy_id": "momentum", **body}
    payload = {k: v for k, v in payload.items() if v is not MISSING}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://engine",
        headers={"Authorization": f"Bearer {TEST_API_TOKEN}"},
    ) as client:
        return await client.post("/api/signals/webhook", json=payload)


async def _untouched(ex: PaperTradingExecutor) -> bool:
    return (await ex.get_balance())["USDT"] == START and not await ex.fetch_positions()


class TestSecret:
    @pytest.mark.parametrize("configured", [None, ""])
    async def test_no_secret_configured_disables_the_webhook(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, configured: str | None
    ) -> None:
        _secret(monkeypatch, configured)
        resp = await _post(secret="", quantity="0.01")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "webhook_disabled"
        assert resp.json()["detail"]["state"] == "unavailable"
        assert await _untouched(sessions["momentum"])

    @pytest.mark.parametrize("presented", ["", "wrong", SECRET[:-1], SECRET + "x"])
    async def test_wrong_secret_is_401_and_places_nothing(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, presented: str
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(secret=presented, quantity="0.01")
        assert resp.status_code == 401
        assert resp.json()["detail"] == {
            "code": "invalid_webhook_secret",
            "message": "Invalid webhook secret",
            "state": "unauthenticated",
        }
        assert await _untouched(sessions["momentum"])

    def test_secret_is_compared_in_constant_time(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[bytes, bytes]] = []
        real = hmac.compare_digest

        def spy(a: bytes, b: bytes) -> bool:
            calls.append((a, b))
            return real(a, b)

        monkeypatch.setattr(signals.hmac, "compare_digest", spy)
        _secret(monkeypatch, SECRET)
        wrong = secrets.token_hex(16)
        resp = TestClient(app).post(
            "/api/signals/webhook",
            json={"side": "buy", "strategy_id": "momentum", "secret": wrong, "quantity": "0.01"},
        )
        assert resp.status_code == 401
        assert (wrong.encode(), SECRET.encode()) in calls


class TestTargeting:
    async def test_correct_secret_trades_only_the_named_strategy(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(secret=SECRET, quantity="0.01")
        assert resp.status_code == 200, resp.text
        positions = await sessions["momentum"].fetch_positions("BTCUSDT")
        assert [(p["side"], p["contracts"]) for p in positions] == [("long", 0.01)]
        # BUY 0.01 at 100 x 1.001 = 100.1, fee 0.1%: 500 - 1.001 - 0.001001
        assert (await sessions["momentum"].get_balance())["USDT"] == Decimal("498.997999")
        stops = await sessions["momentum"].fetch_open_orders("BTCUSDT")
        assert [(o["type"], o["amount"], o["stop_price"]) for o in stops] == [
            ("STOP_MARKET", 0.01, 95.095)
        ]
        assert await _untouched(sessions["breakout"])

    async def test_unknown_strategy_is_404_and_trades_nothing(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(secret=SECRET, quantity="0.01", strategy_id="nope")
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "session_not_found"
        assert await _untouched(sessions["momentum"])
        assert await _untouched(sessions["breakout"])


class TestValidation:
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param({"strategy_id": MISSING}, id="missing-strategy"),
            pytest.param({"strategy_id": ""}, id="empty-strategy"),
            pytest.param({"quantity": "0"}, id="zero-qty"),
            pytest.param({"quantity": "-1"}, id="negative-qty"),
            pytest.param({"side": "banana"}, id="bad-side"),
            pytest.param({"side": "sell"}, id="spot-short-entry"),
            pytest.param({"quantity": "1e400"}, id="huge-qty"),
            pytest.param({"quantity": "0.0000000000000000000001"}, id="dust-qty"),
        ],
    )
    async def test_bad_payload_is_a_422_envelope_and_reaches_no_executor(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(**{"secret": SECRET, "quantity": "0.01", **body})
        assert resp.status_code == 422
        assert resp.json()["detail"]["state"] == "invalid"
        assert await _untouched(sessions["momentum"])
        assert await _untouched(sessions["breakout"])


class TestExecutionOutcome:
    async def test_exit_closes_the_open_position(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        assert (await _post(secret=SECRET, quantity="0.01")).status_code == 200
        resp = await _post(secret=SECRET, side="sell", action="exit")
        assert resp.status_code == 200, resp.text
        assert await sessions["momentum"].fetch_positions("BTCUSDT") == []

    async def test_exit_without_a_position_is_409_no_position(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(secret=SECRET, side="sell", action="exit")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert (detail["code"], detail["state"]) == ("no_position", "no_data")
        assert await _untouched(sessions["momentum"])

    async def test_entry_without_quantity_is_sized_like_a_strategy_entry(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(secret=SECRET, quantity=MISSING)
        assert resp.status_code == 200, resp.text
        # sleeve 500 x max_position_pct 10 % = 50 USDT at mark 100 -> 0.5 BTC
        positions = await sessions["momentum"].fetch_positions("BTCUSDT")
        assert [p["contracts"] for p in positions] == [0.5]

    async def test_risk_rejection_is_422(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = await _post(secret=SECRET, quantity="3")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert (detail["code"], detail["check"]) == ("risk_rejected", "position_size")
        assert await _untouched(sessions["momentum"])

    async def test_executor_failure_is_502_not_a_validation_error(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def exchange_down(**_: Any) -> None:
            raise RuntimeError("exchange down")

        _secret(monkeypatch, SECRET)
        monkeypatch.setattr(sessions["momentum"], "create_order", exchange_down)
        resp = await _post(secret=SECRET, quantity="0.01")
        assert resp.status_code == 502
        detail = resp.json()["detail"]
        assert (detail["code"], detail["state"]) == ("execution_failed", "unavailable")
        assert await _untouched(sessions["momentum"])


def test_telegram_endpoint_is_gone() -> None:
    assert "/api/signals/telegram" not in app.openapi()["paths"]
    resp = TestClient(app).post("/api/signals/telegram", json={"text": "BUY BTCUSDT"})
    assert resp.status_code == 404
