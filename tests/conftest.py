"""Shared test fixtures, markers, and infrastructure setup."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path

import pytest

pytest_plugins = ["tests.infra", "pytester"]

REQUIRE_INFRA_ENV = "HYDRA_REQUIRE_INFRA"
_TIER_MARKERS = ("integration", "e2e")


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: pure unit test, no infrastructure")
    config.addinivalue_line("markers", "integration: requires TimescaleDB + Redis (testcontainers)")
    config.addinivalue_line("markers", "e2e: full pipeline test, all infrastructure")
    config.addinivalue_line("markers", "performance: benchmark test, may be slow")
    config.addinivalue_line("markers", "slow: long-running test")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """With HYDRA_REQUIRE_INFRA=1, a skipped integration or e2e test is reported as failed."""
    report = yield
    if (
        report.skipped
        and not hasattr(report, "wasxfail")
        and os.environ.get(REQUIRE_INFRA_ENV) == "1"
        and any(item.get_closest_marker(m) for m in _TIER_MARKERS)
    ):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        reason = reason.removeprefix("Skipped: ")
        report.outcome = "failed"
        report.longrepr = f"{REQUIRE_INFRA_ENV}=1 but the test was skipped: {reason}"
    return report


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
