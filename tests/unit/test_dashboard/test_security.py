"""Who the API trusts: Remote-User only from trusted proxy peers, bearer token, dev mode."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Annotated, Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from structlog.testing import capture_logs
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from hydra.core.config import HydraConfig, load_config
from hydra.core.logging import get_logger
from hydra.dashboard import security
from hydra.dashboard.api import app, build_uvicorn_config
from hydra.dashboard.security import (
    Principal,
    audit,
    current_principal,
    install_security,
    log_auth_posture,
)

from .conftest import TEST_API_TOKEN

EDGE = "10.10.16.0/24"
TRAEFIK = ("10.10.16.2", 41000)
OUTSIDER = ("10.10.5.9", 41000)
PROTECTED = "/api/portfolio/positions"

Bind = Callable[..., HydraConfig]


@pytest.fixture()
def default_api_auth() -> None:
    """No default bearer: every request here carries exactly the headers the test sets."""


@pytest.fixture()
def proxy_cfg(bind_api_config: Bind) -> HydraConfig:
    return bind_api_config(
        HYDRA_API__AUTH_MODE="proxy",
        HYDRA_API__TRUSTED_PROXY_CIDRS=EDGE,
        HYDRA_API_TOKEN=TEST_API_TOKEN,
    )


def _client(peer: tuple[str, int] = ("testclient", 50000)) -> TestClient:
    return TestClient(app, client=peer)


def _assert_401(resp: Any, code: str = "unauthenticated") -> None:
    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert (detail["code"], detail["state"]) == (code, "unauthenticated")
    assert resp.headers["www-authenticate"] == "Bearer"


def _assert_passed_auth(resp: Any) -> None:
    """The request reached the route: no pool in unit tests, so the route itself says 503."""
    assert resp.status_code == 503
    assert resp.json()["detail"]["code"] == "database_unavailable"


class TestProxyPeerTrust:
    @pytest.mark.usefixtures("proxy_cfg")
    @pytest.mark.parametrize("peer", [("testclient", 50000), OUTSIDER, ("172.22.0.5", 1)])
    def test_remote_user_from_untrusted_peer_is_401(self, peer: tuple[str, int]) -> None:
        _assert_401(_client(peer).get(PROTECTED, headers={"Remote-User": "soma"}))

    @pytest.mark.usefixtures("proxy_cfg")
    def test_remote_user_from_trusted_peer_reaches_the_route(self) -> None:
        _assert_passed_auth(_client(TRAEFIK).get(PROTECTED, headers={"Remote-User": "soma"}))

    @pytest.mark.usefixtures("proxy_cfg")
    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_remote_user_from_trusted_peer_is_401(self, value: str) -> None:
        _assert_401(_client(TRAEFIK).get(PROTECTED, headers={"Remote-User": value}))

    @pytest.mark.usefixtures("proxy_cfg")
    def test_trusted_peer_without_remote_user_is_401(self) -> None:
        _assert_401(_client(TRAEFIK).get(PROTECTED))

    @pytest.mark.usefixtures("proxy_cfg")
    def test_forwarded_headers_cannot_spoof_the_peer(self) -> None:
        resp = _client(OUTSIDER).get(
            PROTECTED,
            headers={
                "Remote-User": "soma",
                "X-Forwarded-For": TRAEFIK[0],
                "X-Real-IP": TRAEFIK[0],
                "Forwarded": f"for={TRAEFIK[0]}",
            },
        )
        _assert_401(resp)

    @pytest.mark.usefixtures("proxy_cfg")
    def test_duplicate_remote_user_is_rejected(self) -> None:
        resp = _client(TRAEFIK).get(
            PROTECTED, headers=[("Remote-User", "soma"), ("Remote-User", "admin")]
        )
        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "duplicate_header"

    def test_token_mode_ignores_remote_user(self, bind_api_config: Bind) -> None:
        bind_api_config(
            HYDRA_API__AUTH_MODE="token",
            HYDRA_API__TRUSTED_PROXY_CIDRS=EDGE,
            HYDRA_API_TOKEN=TEST_API_TOKEN,
        )
        _assert_401(_client(TRAEFIK).get(PROTECTED, headers={"Remote-User": "soma"}))


class TestBearerToken:
    @pytest.mark.usefixtures("proxy_cfg")
    @pytest.mark.parametrize("peer", [("testclient", 50000), OUTSIDER, TRAEFIK])
    def test_correct_token_from_any_peer_passes(self, peer: tuple[str, int]) -> None:
        resp = _client(peer).get(PROTECTED, headers={"Authorization": f"Bearer {TEST_API_TOKEN}"})
        _assert_passed_auth(resp)

    @pytest.mark.usefixtures("proxy_cfg")
    @pytest.mark.parametrize(
        "header", [f"Bearer {TEST_API_TOKEN}x", f"Bearer {TEST_API_TOKEN[:-1]}", "Bearer "]
    )
    def test_wrong_token_is_401_even_from_a_trusted_peer(self, header: str) -> None:
        resp = _client(TRAEFIK).get(
            PROTECTED, headers={"Authorization": header, "Remote-User": "soma"}
        )
        _assert_401(resp, "invalid_token")

    def test_bearer_without_a_configured_token_is_401(self, bind_api_config: Bind) -> None:
        bind_api_config(HYDRA_API__AUTH_MODE="proxy", HYDRA_API__TRUSTED_PROXY_CIDRS=EDGE)
        resp = _client().get(PROTECTED, headers={"Authorization": f"Bearer {TEST_API_TOKEN}"})
        _assert_401(resp, "invalid_token")


class TestFailClosed:
    def test_proxy_mode_without_cidrs_or_token_refuses_everyone(
        self, bind_api_config: Bind
    ) -> None:
        bind_api_config(HYDRA_API__AUTH_MODE="proxy")
        _assert_401(
            _client(TRAEFIK).get(PROTECTED, headers={"Remote-User": "soma"}),
            "auth_not_configured",
        )

    def test_no_loaded_config_refuses_everyone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(app.state, "config", None)
        _assert_401(_client().get(PROTECTED), "auth_not_configured")
        assert _client().get("/health").status_code == 503

    @pytest.mark.parametrize("env", ["HYDRA_API_TOKEN", "HYDRA_API__CORS_ORIGINS"])
    def test_weak_settings_stop_startup(self, env: str, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(env, "short" if env == "HYDRA_API_TOKEN" else "*")
        with pytest.raises(ValidationError):
            load_config()


def _probe_app(cfg: HydraConfig) -> FastAPI:
    probe = FastAPI()
    install_security(probe)
    probe.state.config = cfg

    @probe.get("/whoami")
    async def whoami(
        principal: Annotated[Principal, Depends(current_principal)],
    ) -> dict[str, str]:
        return {"kind": principal.kind, "name": principal.name}

    return probe


class TestPrincipal:
    def test_user_principal_carries_the_remote_user(self, proxy_cfg: HydraConfig) -> None:
        resp = TestClient(_probe_app(proxy_cfg), client=TRAEFIK).get(
            "/whoami", headers={"Remote-User": "soma"}
        )
        assert resp.json() == {"kind": "user", "name": "soma"}

    def test_service_principal(self, proxy_cfg: HydraConfig) -> None:
        resp = TestClient(_probe_app(proxy_cfg)).get(
            "/whoami", headers={"Authorization": f"Bearer {TEST_API_TOKEN}"}
        )
        assert resp.json() == {"kind": "service", "name": "api-token"}

    def test_dev_mode_trusts_everyone_as_dev(self, bind_api_config: Bind) -> None:
        cfg = bind_api_config(HYDRA_API__AUTH_MODE="dev")
        resp = TestClient(_probe_app(cfg), client=OUTSIDER).get("/whoami")
        assert resp.json() == {"kind": "dev", "name": "dev"}
        _assert_passed_auth(_client(OUTSIDER).get(PROTECTED))


class TestStartup:
    def test_uvicorn_never_rewrites_the_peer_from_forwarded_headers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
        config = build_uvicorn_config()
        assert config.proxy_headers is False
        config.load()
        assert not isinstance(config.loaded_app, ProxyHeadersMiddleware)

    def test_dev_mode_is_logged_critical(
        self, bind_api_config: Bind, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = bind_api_config(HYDRA_API__AUTH_MODE="dev")
        with caplog.at_level(logging.INFO, logger="hydra.dashboard.security"):
            log_auth_posture(cfg)
        assert [r.levelno for r in caplog.records] == [logging.CRITICAL]

    def test_unconfigured_proxy_mode_is_logged_error(
        self, bind_api_config: Bind, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = bind_api_config(HYDRA_API__AUTH_MODE="proxy")
        with caplog.at_level(logging.INFO, logger="hydra.dashboard.security"):
            log_auth_posture(cfg)
        assert [r.levelno for r in caplog.records] == [logging.ERROR]
        assert "auth_not_configured" in caplog.records[0].getMessage()

    @pytest.mark.parametrize(
        ("cidrs", "wide"),
        [
            ("10.10.16.0/24", 1),
            ("10.10.16.0/24,fd00::/64", 2),
            ("10.10.16.2/32,fd00::/64", 1),
            ("10.10.16.2/32,fd00::2/128", 0),
        ],
    )
    def test_trusting_more_than_one_host_is_warned(
        self, bind_api_config: Bind, caplog: pytest.LogCaptureFixture, cidrs: str, wide: int
    ) -> None:
        cfg = bind_api_config(HYDRA_API__AUTH_MODE="proxy", HYDRA_API__TRUSTED_PROXY_CIDRS=cidrs)
        with caplog.at_level(logging.INFO, logger="hydra.dashboard.security"):
            log_auth_posture(cfg)
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == (1 if wide else 0)
        for message in warnings:
            assert message.startswith(f"{wide} api.trusted_proxy_cidrs entries")
            assert "/32" in message

    def test_posture_is_logged_when_the_lifespan_finishes(
        self, bind_api_config: Bind, caplog: pytest.LogCaptureFixture
    ) -> None:
        cfg = bind_api_config(HYDRA_API__AUTH_MODE="dev")
        probe = _probe_app(cfg)
        with (
            caplog.at_level(logging.INFO, logger="hydra.dashboard.security"),
            TestClient(probe),
        ):
            pass
        assert any(r.levelno == logging.CRITICAL for r in caplog.records)


def test_audit_line_carries_principal_and_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    with capture_logs() as logs:
        # a logger cached by an earlier audit() call would bypass capture_logs
        monkeypatch.setattr(security, "audit_logger", get_logger("hydra.audit"))
        audit(Principal("user", "soma"), "kill_switch.activate", sessions_stopped=2)
    assert logs == [
        {
            "event": "audit",
            "log_level": "info",
            "action": "kill_switch.activate",
            "principal": "soma",
            "principal_kind": "user",
            "sessions_stopped": 2,
        }
    ]
