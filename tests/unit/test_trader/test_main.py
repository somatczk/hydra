"""The trader process entry point's healthcheck: the heartbeat file freshness (D5)."""

from __future__ import annotations

import os
import time
from pathlib import Path

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
