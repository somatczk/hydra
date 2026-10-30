"""The trader process entry point's healthcheck and Redis fail-fast (D5, P7-E)."""

from __future__ import annotations

import os
import socket
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from hydra.core.config import EventsConfig, HydraConfig
from hydra.trader import main as trader_main


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()


def test_a_fresh_heartbeat_passes(tmp_path: Path) -> None:
    path = tmp_path / "hydra-trader.heartbeat"
    trader_main.touch_heartbeat(str(path))
    assert trader_main.check(str(path)) == 0


def test_a_missing_heartbeat_fails(tmp_path: Path) -> None:
    assert trader_main.check(str(tmp_path / "gone.heartbeat")) == 1


def test_a_stale_heartbeat_fails(tmp_path: Path) -> None:
    path = tmp_path / "hydra-trader.heartbeat"
    _touch(path)
    stale = time.time() - trader_main.HEARTBEAT_MAX_AGE_S - 1
    os.utime(path, (stale, stale))
    assert trader_main.check(str(path)) == 1


def test_the_check_watches_the_traders_own_heartbeat_file() -> None:
    assert trader_main.HEARTBEAT_PATH.endswith("hydra-trader.heartbeat")
    assert "hydra-worker" not in trader_main.HEARTBEAT_PATH


# ---------------------------------------------------------------------------
# The opt-in Redis fan-out (P7-E): with the flag on, Redis is tier-1 at startup
# ---------------------------------------------------------------------------


def _closed_redis_url() -> str:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return f"redis://127.0.0.1:{probe.getsockname()[1]}"


def _config(bus: str, redis_url: str | None = None) -> Any:
    kwargs: dict[str, Any] = {"events": EventsConfig(bus=bus)}
    if redis_url is not None:
        kwargs["REDIS_URL"] = SecretStr(redis_url)
    return HydraConfig(**kwargs)


async def test_the_fanout_flag_off_never_pings_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def explode(url: str, *, budget_s: float = 30.0) -> None:
        raise AssertionError("the flag is off: Redis must not be pinged")

    import hydra.core.event_bus as event_bus

    monkeypatch.setattr(event_bus, "require_redis", explode)
    await trader_main._require_redis(_config("memory"))


async def test_the_fanout_flag_on_refuses_startup_when_redis_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import redis

    monkeypatch.setattr(trader_main, "REDIS_CONNECT_BUDGET_S", 0.2)
    with pytest.raises(redis.exceptions.ConnectionError):
        await trader_main._require_redis(_config("redis", _closed_redis_url()))
