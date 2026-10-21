"""Shared test fixtures, markers, and infrastructure setup."""

from __future__ import annotations

import os
from collections.abc import Generator
from pathlib import Path
from typing import Any

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


def _require_infra() -> bool:
    return os.environ.get(REQUIRE_INFRA_ENV) == "1"


def _tier_of(nodeid: str) -> str | None:
    """The tier a node belongs to by its directory: ``tests/integration`` or ``tests/e2e``."""
    parts = Path(nodeid.split("::", 1)[0]).parts
    return next((m for m in _TIER_MARKERS if m in parts[:-1]), None)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test by its tier directory, so ``-m integration``/``-m e2e`` cannot drop one."""
    for item in items:
        tier = _tier_of(item.nodeid)
        if tier and not item.get_closest_marker(tier):
            item.add_marker(tier)


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(
    collector: pytest.Collector,
) -> Generator[None, pytest.CollectReport, pytest.CollectReport]:
    """With HYDRA_REQUIRE_INFRA=1, a tier module skipped at collection time is an error."""
    report = yield
    if report.skipped and _require_infra() and _tier_of(report.nodeid):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        reason = reason.removeprefix("Skipped: ")
        report.outcome = "failed"
        report.longrepr = f"{REQUIRE_INFRA_ENV}=1 but the module was skipped: {reason}"
    return report


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """With HYDRA_REQUIRE_INFRA=1, a skipped integration or e2e test is reported as failed."""
    report = yield
    if (
        report.skipped
        and not hasattr(report, "wasxfail")
        and _require_infra()
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
    monkeypatch.setattr("hydra.services.strategies._CONFIG_DIR", path)
    monkeypatch.setattr("hydra.dashboard.routes.templates._STRATEGIES_DIR", path)
    monkeypatch.setattr("hydra.execution.session_manager._STRATEGY_DIR", path)
    monkeypatch.setenv("HYDRA_ML__MODELS_DIR", str(tmp_path / "models"))
    return path


@pytest.fixture(autouse=True)
def offline_market_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """Backtests load the fixture's spot markets (lot step 0.00001), never public Binance."""
    from hydra.risk.market_rules import MarketRulesCache
    from tests.fixtures.binance_kline import binance_markets

    async def loader(exchange_id: str) -> list[dict[str, Any]]:
        return list(binance_markets().values())

    monkeypatch.setattr("hydra.backtest.runner._MARKET_RULES", MarketRulesCache(loader=loader))
