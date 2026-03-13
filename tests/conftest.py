"""Shared test fixtures, markers, and infrastructure setup."""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    """Register custom markers."""
    config.addinivalue_line("markers", "unit: pure unit test, no infrastructure")
    config.addinivalue_line("markers", "integration: requires TimescaleDB + Redis (testcontainers)")
    config.addinivalue_line("markers", "e2e: full pipeline test, all infrastructure")
    config.addinivalue_line("markers", "performance: benchmark test, may be slow")
    config.addinivalue_line("markers", "slow: long-running test")
