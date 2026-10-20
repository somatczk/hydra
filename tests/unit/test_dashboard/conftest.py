"""Dashboard tests run with the repo config loaded the way the lifespan loads it."""

from __future__ import annotations

import secrets
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hydra.core.config import HydraConfig, load_config
from hydra.dashboard import deps
from hydra.dashboard.api import app

TEST_API_TOKEN = secrets.token_urlsafe(32)
_AUTH_ENV = (
    "HYDRA_API__AUTH_MODE",
    "HYDRA_API__TRUSTED_PROXY_CIDRS",
    "HYDRA_API__CORS_ORIGINS",
    "HYDRA_API_TOKEN",
)


@pytest.fixture(autouse=True)
def hydra_config(strategies_dir: Path, monkeypatch: pytest.MonkeyPatch) -> HydraConfig:
    for name in ("DATABASE_URL", "REDIS_URL", "HYDRA_ENV", "HYDRA_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HYDRA_API_TOKEN", TEST_API_TOKEN)
    cfg = load_config()
    monkeypatch.setattr(deps, "_bound", cfg)
    monkeypatch.setattr(app.state, "config", cfg)
    return cfg


@pytest.fixture(autouse=True)
def default_api_auth(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """TestClient(app) calls the real auth path as the bearer-token service principal.

    Security tests override this fixture with a no-op to send exactly the headers they choose.
    """
    original = TestClient.__init__

    def init(self: TestClient, asgi_app: Any, *args: Any, **kwargs: Any) -> None:
        if asgi_app is app:
            bearer = {"Authorization": f"Bearer {TEST_API_TOKEN}"}
            kwargs["headers"] = {**bearer, **(kwargs.get("headers") or {})}
        original(self, asgi_app, *args, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)
    yield


@pytest.fixture()
def bind_api_config(monkeypatch: pytest.MonkeyPatch) -> Callable[..., HydraConfig]:
    """Reload the config with exactly the given auth env and bind it to the real app."""

    def bind(**env: str) -> HydraConfig:
        for name in _AUTH_ENV:
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        cfg = load_config()
        monkeypatch.setattr(deps, "_bound", cfg)
        monkeypatch.setattr(app.state, "config", cfg)
        return cfg

    return bind
