"""bar_coverage: the stored-range read behind the MCP data_coverage tool.

The repository seam (MarketDataRepository.get_bar_range) is exercised against a real
TimescaleDB in tests/integration/test_mcp_tools_pg.py; here the pool is a record stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from hydra.core.types import Timeframe
from hydra.services.data import bar_coverage
from hydra.services.errors import ServiceError


class _CoverageConn:
    """Serves one MIN/MAX/COUNT row for the coverage query; records the bound parameters."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.args: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM ts.ohlcv_1m" in query
        self.args = args
        return self.row


class _Pool:
    def __init__(self, conn: _CoverageConn) -> None:
        self.conn = conn

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> _CoverageConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _row(n: int) -> dict[str, Any]:
    return {
        "first_ts": datetime(2026, 1, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 2, 23, tzinfo=UTC),
        "n": n,
    }


async def test_returns_the_exact_stored_range() -> None:
    conn = _CoverageConn(_row(48))
    result = await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1)
    assert result == {
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-02T23:00:00+00:00",
        "bar_count": 48,
    }


async def test_reads_the_whole_stored_range_not_a_window() -> None:
    """The coverage query passes NULL bounds: first/last come from everything stored."""
    conn = _CoverageConn(_row(3))
    await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1)
    assert conn.args == ("binance", "BTCUSDT", "1h", None, None)


async def test_no_stored_bars_is_a_404_no_stored_data_envelope() -> None:
    conn = _CoverageConn(_row(0))
    with pytest.raises(ServiceError) as err:
        await bar_coverage(_Pool(conn), "binance", "ETHUSDT", Timeframe.H1)
    assert err.value.status_code == 404
    assert err.value.detail == {
        "code": "no_stored_data",
        "message": "No stored bars for binance ETHUSDT 1h",
        "state": "no_data",
        "exchange_id": "binance",
        "symbol": "ETHUSDT",
        "timeframe": "1h",
    }


async def test_no_pool_is_a_503() -> None:
    with pytest.raises(ServiceError) as err:
        await bar_coverage(None, "binance", "BTCUSDT", Timeframe.H1)
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "database_unavailable"
