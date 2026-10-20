"""Dashboard tests run with the repo config loaded the way the lifespan loads it."""

from __future__ import annotations

from pathlib import Path

import pytest

from hydra.core.config import HydraConfig, load_config
from hydra.dashboard import deps
from hydra.dashboard.api import app


@pytest.fixture(autouse=True)
def hydra_config(strategies_dir: Path, monkeypatch: pytest.MonkeyPatch) -> HydraConfig:
    for name in ("DATABASE_URL", "REDIS_URL", "HYDRA_ENV", "HYDRA_CONFIG_DIR"):
        monkeypatch.delenv(name, raising=False)
    cfg = load_config()
    monkeypatch.setattr(deps, "_bound", cfg)
    monkeypatch.setattr(app.state, "config", cfg)
    return cfg
