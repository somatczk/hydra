"""Signal webhook: fail-closed secret, constant-time compare, one strategy's session only.

Sessions hold real PaperTradingExecutors; only the engine's session table is set up by hand.
"""

from __future__ import annotations

import hmac
import secrets
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hydra.core.config import HydraConfig
from hydra.dashboard.api import app
from hydra.dashboard.routes import signals
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.execution.session_manager import SessionManager, TradingSession

SECRET = secrets.token_hex(16)
START = Decimal("10000")


def _session(session_id: str, strategy_id: str) -> TradingSession:
    executor = PaperTradingExecutor(initial_balances={"USDT": START}, strategy_id=strategy_id)
    executor.set_market_price("BTCUSDT", Decimal("60000"))
    session = TradingSession(
        session_id=session_id,
        strategy_id=strategy_id,
        trading_mode="paper",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
    )
    session._executor = executor
    return session


@pytest.fixture()
def sessions(hydra_config: HydraConfig, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    mgr = SessionManager(db_pool=None, config=hydra_config)
    made = {"momentum": _session("s-mom", "momentum"), "breakout": _session("s-brk", "breakout")}
    for s in made.values():
        mgr._sessions[s.session_id] = s
    monkeypatch.setattr(app.state, "session_manager", mgr)
    return {k: s._executor for k, s in made.items()}


def _secret(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    cfg = {} if value is None else {"webhook_secret": value}
    monkeypatch.setattr(app.state, "system_config", cfg)


MISSING = object()


def _post(**body: Any) -> Any:
    payload = {"symbol": "BTCUSDT", "side": "buy", "strategy_id": "momentum", **body}
    payload = {k: v for k, v in payload.items() if v is not MISSING}
    return TestClient(app).post("/api/signals/webhook", json=payload)


async def _untouched(executor: PaperTradingExecutor) -> bool:
    return (await executor.get_balance())["USDT"] == START and not await executor.fetch_positions()


class TestSecret:
    @pytest.mark.parametrize("configured", [None, ""])
    async def test_no_secret_configured_disables_the_webhook(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, configured: str | None
    ) -> None:
        _secret(monkeypatch, configured)
        resp = _post(secret="", quantity="0.01")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "webhook_disabled"
        assert resp.json()["detail"]["state"] == "unavailable"
        assert await _untouched(sessions["momentum"])

    @pytest.mark.parametrize("presented", ["", "wrong", SECRET[:-1], SECRET + "x"])
    async def test_wrong_secret_is_401_and_places_nothing(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, presented: str
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = _post(secret=presented, quantity="0.01")
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
        assert _post(secret=wrong, quantity="0.01").status_code == 401
        assert (wrong.encode(), SECRET.encode()) in calls


class TestTargeting:
    async def test_correct_secret_trades_only_the_named_strategy(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = _post(secret=SECRET, quantity="0.01")
        assert resp.status_code == 200, resp.text
        positions = await sessions["momentum"].fetch_positions("BTCUSDT")
        assert [(p["side"], p["contracts"]) for p in positions] == [("long", 0.01)]
        assert (await sessions["momentum"].get_balance())["USDT"] < START
        assert await _untouched(sessions["breakout"])

    async def test_unknown_strategy_is_404_and_trades_nothing(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = _post(secret=SECRET, quantity="0.01", strategy_id="nope")
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
        ],
    )
    async def test_bad_payload_is_a_422_envelope_and_reaches_no_executor(
        self, sessions: dict[str, Any], monkeypatch: pytest.MonkeyPatch, body: dict[str, Any]
    ) -> None:
        _secret(monkeypatch, SECRET)
        resp = _post(**{"secret": SECRET, "quantity": "0.01", **body})
        assert resp.status_code == 422
        assert resp.json()["detail"]["state"] == "invalid"
        assert await _untouched(sessions["momentum"])
        assert await _untouched(sessions["breakout"])


def test_telegram_endpoint_is_gone() -> None:
    assert "/api/signals/telegram" not in app.openapi()["paths"]
    resp = TestClient(app).post("/api/signals/telegram", json={"text": "BUY BTCUSDT"})
    assert resp.status_code == 404
