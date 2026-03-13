"""Shared fixtures for integration tests.

Integration tests may require external services (Redis, PostgreSQL).
Tests that need unavailable services should skip gracefully.
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def redis_url() -> str:
    """Default Redis URL for integration tests."""
    return "redis://localhost:6379"
