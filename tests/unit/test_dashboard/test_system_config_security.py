"""System config: write-only encrypted secrets, validated values, webhook host allowlist."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from hydra.core.encryption import encrypt
from hydra.dashboard import lifespan
from hydra.dashboard.routes._db import install_error_handlers
from hydra.dashboard.routes.signals import _get_webhook_secret
from hydra.dashboard.routes.system import router

from .system_db import FakeSystemDb

_SLACK = "https://hooks.slack.com/services/T000/B000/xyz"
_DISCORD = "https://discord.com/api/webhooks/1/abc"


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


@pytest.fixture()
def db() -> FakeSystemDb:
    return FakeSystemDb()


def _make_app(pool: Any = None) -> FastAPI:
    app = FastAPI()
    app.state.db_pool = pool
    app.state.system_config = None
    app.state.session_manager = None
    install_error_handlers(app)
    app.include_router(router)
    return app


def _webhook_secret(app: FastAPI) -> str:
    return _get_webhook_secret(Request({"type": "http", "app": app}))


def _store_secret(db: FakeSystemDb, key: str, value: str) -> None:
    db.config[key] = {"value": json.dumps(encrypt(value)), "updated_by": "test"}


class TestWriteOnlySecrets:
    def test_secrets_write_only(self, db: FakeSystemDb) -> None:
        client = TestClient(_make_app(db))
        resp = client.put(
            "/api/system/config",
            json={"webhook_secret": "s3cr3t-XYZ", "slack_webhook_url": _SLACK},
        )
        assert resp.status_code == 200
        for body in (resp.text, client.get("/api/system/config").text):
            assert "s3cr3t-XYZ" not in body
            assert "hooks.slack.com/services" not in body
        data = client.get("/api/system/config").json()
        assert data["webhook_secret_set"] is True
        assert data["slack_webhook_url_set"] is True
        assert data["discord_webhook_url_set"] is False
        assert "webhook_secret" not in data
        assert "slack_webhook_url" not in data

    def test_untouched_secret_survives_other_update(self, db: FakeSystemDb) -> None:
        app = _make_app(db)
        client = TestClient(app)
        client.put("/api/system/config", json={"webhook_secret": "s3cr3t-XYZ"})

        assert client.put("/api/system/config", json={"paper_capital": 5000}).status_code == 200
        assert _webhook_secret(app) == "s3cr3t-XYZ"
        assert app.state.system_config["paper_capital"] == 5000

        resp = client.put("/api/system/config", json={"webhook_secret": ""})
        assert resp.json()["webhook_secret_set"] is False
        assert _webhook_secret(app) == ""

    async def test_webhook_secret_survives_a_restart(self, db: FakeSystemDb) -> None:
        TestClient(_make_app(db)).put("/api/system/config", json={"webhook_secret": "s3cr3t-XYZ"})
        assert "s3cr3t-XYZ" not in json.dumps(db.config)

        restarted = _make_app(db)
        await lifespan._load_system_config(restarted)
        assert _webhook_secret(restarted) == "s3cr3t-XYZ"
        assert restarted.state.system_config["paper_capital"] == 10000

    async def test_secret_under_a_lost_key_fails_closed_after_restart(
        self, db: FakeSystemDb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        TestClient(_make_app(db)).put("/api/system/config", json={"webhook_secret": "s3cr3t-XYZ"})
        monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())

        restarted = _make_app(db)
        await lifespan._load_system_config(restarted)
        assert _webhook_secret(restarted) == ""


class TestValidation:
    @pytest.mark.parametrize(
        "payload",
        [
            {"live_capital": 0},
            {"live_capital": -1},
            {"live_capital": "inf"},
            {"live_capital": "nan"},
            {"paper_capital": 0},
            {"paper_capital": -1},
            {"paper_capital": "inf"},
            {"paper_capital": "nan"},
            {"max_concurrent_strategies": 0},
            {"max_concurrent_strategies": 101},
        ],
    )
    def test_invalid_values_rejected_and_state_unchanged(
        self, db: FakeSystemDb, payload: dict[str, Any]
    ) -> None:
        app = _make_app(db)
        client = TestClient(app)
        client.put("/api/system/config", json={"paper_capital": 2500, "trading_mode": "paper"})
        before = dict(app.state.system_config)
        stored = json.dumps(db.config)

        resp = client.put("/api/system/config", json=payload)
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["code"] == "validation_error"
        assert detail["state"] == "invalid"
        field = next(iter(payload))
        assert detail["message"].startswith(f"{field}: ")
        assert detail["errors"][0]["loc"] == ["body", field]
        assert app.state.system_config == before
        assert json.dumps(db.config) == stored

    def test_valid_values_accepted(self, db: FakeSystemDb) -> None:
        client = TestClient(_make_app(db))
        resp = client.put(
            "/api/system/config",
            json={
                "trading_mode": "live",
                "paper_capital": 2500.5,
                "max_concurrent_strategies": 100,
                "live_capital": 5000,
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # must-fix #3: trading_mode is not writable; the row stays 'paper' but live_capital persists
        assert data["trading_mode"] == "paper"
        assert data["paper_capital"] == 2500.5
        assert data["max_concurrent_strategies"] == 100
        assert db.value("live_capital") == 5000

    def test_bad_stored_trading_mode_is_503_not_coerced(self, db: FakeSystemDb) -> None:
        db.config["trading_mode"]["value"] = '"testnet"'
        resp = TestClient(_make_app(db)).get("/api/system/config")
        assert resp.status_code == 503
        assert resp.json()["detail"]["code"] == "system_config_invalid"


class TestWebhookAllowlist:
    @pytest.mark.parametrize(
        ("field", "url"),
        [
            ("slack_webhook_url", "http://hooks.slack.com/services/T/B/x"),
            ("slack_webhook_url", "https://hooks.slack.com.evil.com/services/T/B/x"),
            ("discord_webhook_url", "https://discord.com@evil.com/api/webhooks/x"),
            ("discord_webhook_url", "https://evil.com/?h=discord.com"),
            ("discord_webhook_url", "https://169.254.169.254/"),
            ("discord_webhook_url", "https://discord.com:8443/api/webhooks/x"),
            ("discord_webhook_url", "https://user:pw@discord.com/api/webhooks/x"),
            ("discord_webhook_url", "https://discord.com/not-a-webhook"),
            ("discord_webhook_url", "https://discord.com/api/webhooks/../../users/@me"),
            ("discord_webhook_url", "https://discord.com/api/webhooks/./x"),
            ("discord_webhook_url", "https://discord.com/api/webhooks/%2e%2e/%2E%2E/users/@me"),
            ("slack_webhook_url", "https://hooks.slack.com/services/../../x"),
            ("slack_webhook_url", _DISCORD),
            ("discord_webhook_url", _SLACK),
        ],
    )
    def test_rejected(self, db: FakeSystemDb, field: str, url: str) -> None:
        app = _make_app(db)
        resp = TestClient(app).put("/api/system/config", json={field: url})
        assert resp.status_code == 422
        assert url not in resp.text
        assert app.state.system_config is None
        assert field not in db.config

    def test_dot_segment_blocked_on_send(
        self, db: FakeSystemDb, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sent: list[str] = []

        async def _post(self: httpx.AsyncClient, url: str, **_: Any) -> httpx.Response:
            sent.append(url)
            return httpx.Response(204, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", _post)
        _store_secret(db, "discord_webhook_url", "https://discord.com/api/webhooks/../../users/@me")
        resp = TestClient(_make_app(db)).post("/api/system/notifications/test")
        assert resp.status_code == 422
        assert sent == []

    @pytest.mark.parametrize(
        ("field", "url"),
        [
            ("discord_webhook_url", _DISCORD),
            ("slack_webhook_url", _SLACK),
            ("discord_webhook_url", "https://discord.com/api/webhooks/1/a.b..c"),
        ],
    )
    def test_accepted(self, db: FakeSystemDb, field: str, url: str) -> None:
        app = _make_app(db)
        resp = TestClient(app).put("/api/system/config", json={field: url})
        assert resp.status_code == 200
        assert resp.json()[f"{field}_set"] is True
        assert app.state.system_config[field] == url
        assert db.value(field) != url


class TestNotifications:
    @pytest.fixture()
    def outbound(self, monkeypatch: pytest.MonkeyPatch) -> list[str]:
        sent: list[str] = []

        async def _post(self: httpx.AsyncClient, url: str, **_: Any) -> httpx.Response:
            sent.append(url)
            return httpx.Response(204, request=httpx.Request("POST", url))

        monkeypatch.setattr(httpx.AsyncClient, "post", _post)
        return sent

    def test_notifications_test_revalidates(self, db: FakeSystemDb, outbound: list[str]) -> None:
        _store_secret(db, "slack_webhook_url", "http://127.0.0.1:9/x")
        resp = TestClient(_make_app(db)).post("/api/system/notifications/test")
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "invalid_webhook_url"
        assert outbound == []

    def test_one_bad_url_blocks_both_channels(self, db: FakeSystemDb, outbound: list[str]) -> None:
        _store_secret(db, "discord_webhook_url", _DISCORD)
        _store_secret(db, "slack_webhook_url", "https://hooks.slack.com.evil.com/services/x")
        assert TestClient(_make_app(db)).post("/api/system/notifications/test").status_code == 422
        assert outbound == []

    def test_valid_urls_are_sent(self, db: FakeSystemDb, outbound: list[str]) -> None:
        TestClient(_make_app(db)).put(
            "/api/system/config",
            json={"discord_webhook_url": _DISCORD, "slack_webhook_url": _SLACK},
        )
        resp = TestClient(_make_app(db)).post("/api/system/notifications/test")
        assert resp.status_code == 200
        assert resp.json()["discord_sent"] is True
        assert resp.json()["slack_sent"] is True
        assert outbound == [_DISCORD, _SLACK]

    def test_no_urls_sends_nothing(self, db: FakeSystemDb, outbound: list[str]) -> None:
        resp = TestClient(_make_app(db)).post("/api/system/notifications/test")
        assert resp.status_code == 200
        assert resp.json()["discord_sent"] is False
        assert outbound == []

    def test_notifications_get_503_when_db_down(self) -> None:
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(side_effect=ConnectionError("refused"))
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = TestClient(_make_app(pool=pool)).get("/api/system/notifications")
        assert resp.status_code == 503
        assert resp.json()["detail"]["state"] == "unavailable"

    def test_notifications_get_503_without_pool(self) -> None:
        resp = TestClient(_make_app()).get("/api/system/notifications")
        assert resp.status_code == 503

    def test_notifications_get_empty_row_is_200(self) -> None:
        conn = MagicMock()
        conn.fetchrow = AsyncMock(return_value=None)
        pool = MagicMock()
        pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
        pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
        resp = TestClient(_make_app(pool=pool)).get("/api/system/notifications")
        assert resp.status_code == 200
        assert resp.json() == {"preferences": {}}


def test_placeholders_are_gone() -> None:
    from hydra.dashboard.routes import system

    assert not hasattr(system, "_EXCHANGES_PLACEHOLDER")
    assert not hasattr(system, "_HEALTH_PLACEHOLDER")
