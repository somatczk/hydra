"""Shared test fixtures, markers, and infrastructure setup."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: pure unit test, no infrastructure")
    config.addinivalue_line("markers", "integration: requires TimescaleDB + Redis (testcontainers)")
    config.addinivalue_line("markers", "e2e: full pipeline test, all infrastructure")
    config.addinivalue_line("markers", "performance: benchmark test, may be slow")
    config.addinivalue_line("markers", "slow: long-running test")


@pytest.fixture(autouse=True)
def strategies_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect every writable config dir into tmp_path; the default strategies dir is empty."""
    path = tmp_path / "config" / "strategies"
    path.mkdir(parents=True)
    monkeypatch.setattr("hydra.dashboard.routes.strategies._CONFIG_DIR", path)
    monkeypatch.setattr("hydra.dashboard.routes.templates._STRATEGIES_DIR", path)
    monkeypatch.setattr("hydra.execution.session_manager._STRATEGY_DIR", path)
    monkeypatch.setenv("HYDRA_ML__MODELS_DIR", str(tmp_path / "models"))
    return path
