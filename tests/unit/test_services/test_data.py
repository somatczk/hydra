"""bar_coverage and cache_inventory: the stored-data reads behind the MCP tools.

The repository seam (MarketDataRepository.get_bar_range) is exercised against a real
TimescaleDB in tests/integration/test_mcp_tools_pg.py; here the pool is a record stub.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from hydra.core.config import DataConfig
from hydra.core.types import Timeframe
from hydra.services.data import bar_coverage, cache_inventory
from hydra.services.errors import ServiceError


class _CoverageConn:
    """Serves the three coverage reads: the range row, the source split, the ledger row."""

    def __init__(
        self,
        row: dict[str, Any] | None,
        sources: list[dict[str, Any]] | None = None,
        entry: dict[str, Any] | None = None,
    ) -> None:
        self.row = row
        self.sources = sources or []
        self.entry = entry
        self.args: tuple[Any, ...] = ()

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        if "FROM data_cache_entries" in query:
            return self.entry
        assert "FROM ts.ohlcv_1m" in query
        self.args = args
        return self.row

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM ts.ohlcv_1m" in query and "GROUP BY source" in query
        return self.sources


class _InventoryConn:
    """Serves the two inventory reads: the LRU-ordered ledger rows and the totals row."""

    def __init__(self, entries: list[dict[str, Any]], totals: dict[str, Any]) -> None:
        self.entries = entries
        self.totals = totals

    async def fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        assert "FROM data_cache_entries" in query
        if "ORDER BY" in query:
            return self.entries
        return [self.totals]


class _Pool:
    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> Any:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


def _row(n: int) -> dict[str, Any]:
    return {
        "first_ts": datetime(2026, 1, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 2, 23, tzinfo=UTC),
        "n": n,
    }


def _entry(symbol: str, timeframe: str, rows: int, last_used: datetime) -> dict[str, Any]:
    return {
        "exchange": "binance",
        "symbol": symbol,
        "timeframe": timeframe,
        "first_ts": datetime(2025, 12, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 31, 23, tzinfo=UTC),
        "rows": rows,
        "status": "ready",
        "created_at": datetime(2025, 12, 1, tzinfo=UTC),
        "last_fetched_at": datetime(2026, 10, 27, 9, tzinfo=UTC),
        "last_used_at": last_used,
    }


async def test_returns_the_exact_stored_range() -> None:
    conn = _CoverageConn(_row(48))
    result = await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1, 160)
    assert result == {
        "exchange_id": "binance",
        "symbol": "BTCUSDT",
        "timeframe": "1h",
        "first_ts": "2026-01-01T00:00:00+00:00",
        "last_ts": "2026-01-02T23:00:00+00:00",
        "bar_count": 48,
        "sources": {"feed": 0, "auto": 0},
        "estimated_bytes": 48 * 160,
        "cache": None,
    }


async def test_reads_the_whole_stored_range_not_a_window() -> None:
    """The coverage query passes NULL bounds: first/last come from everything stored."""
    conn = _CoverageConn(_row(3))
    await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1, 160)
    assert conn.args == ("binance", "BTCUSDT", "1h", None, None)


async def test_the_source_split_and_the_estimate_come_from_the_rows_and_the_config() -> None:
    conn = _CoverageConn(
        _row(48), sources=[{"source": "feed", "n": 40}, {"source": "auto", "n": 8}]
    )
    result = await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1, 250)
    assert result["sources"] == {"feed": 40, "auto": 8}
    assert result["estimated_bytes"] == 48 * 250


async def test_a_source_value_outside_the_domain_surfaces_verbatim() -> None:
    """The split never hides rows: a stray source counts under its own key."""
    conn = _CoverageConn(_row(5), sources=[{"source": "manual", "n": 5}])
    result = await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1, 160)
    assert result["sources"] == {"manual": 5, "feed": 0, "auto": 0}


async def test_the_cache_ledger_row_shapes_the_coverage_payload() -> None:
    """The ledger block keeps its own advisory rows and requested-coverage range,
    never conflated with the real bar count and range."""
    entry = {
        "status": "ready",
        "rows": 300,
        "first_ts": datetime(2025, 12, 1, tzinfo=UTC),
        "last_ts": datetime(2026, 1, 2, 23, tzinfo=UTC),
        "last_used_at": datetime(2026, 10, 28, 9, tzinfo=UTC),
        "last_fetched_at": datetime(2026, 10, 27, 9, tzinfo=UTC),
    }
    conn = _CoverageConn(_row(348), entry=entry)
    result = await bar_coverage(_Pool(conn), "binance", "BTCUSDT", Timeframe.H1, 160)
    assert result["bar_count"] == 348
    assert result["cache"] == {
        "status": "ready",
        "rows": 300,
        "coverage": {
            "first_ts": "2025-12-01T00:00:00+00:00",
            "last_ts": "2026-01-02T23:00:00+00:00",
        },
        "last_used_at": "2026-10-28T09:00:00+00:00",
        "last_fetched_at": "2026-10-27T09:00:00+00:00",
    }


async def test_no_stored_bars_is_a_404_no_stored_data_envelope() -> None:
    conn = _CoverageConn(_row(0))
    with pytest.raises(ServiceError) as err:
        await bar_coverage(_Pool(conn), "binance", "ETHUSDT", Timeframe.H1, 160)
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
        await bar_coverage(None, "binance", "BTCUSDT", Timeframe.H1, 160)
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "database_unavailable"


async def test_the_inventory_lists_every_dataset_lru_first_with_exact_totals() -> None:
    entries = [
        _entry("ETHUSDT", "1h", 0, datetime(2026, 10, 1, tzinfo=UTC)),
        _entry("ETHUSDT", "1m", 500, datetime(2026, 10, 2, tzinfo=UTC)),
        _entry("BTCUSDT", "1m", 300, datetime(2026, 10, 3, tzinfo=UTC)),
    ]
    conn = _InventoryConn(entries, {"rows": 800, "datasets": 3})
    result = await cache_inventory(
        _Pool(conn), DataConfig(cache_max_gb=0.5, row_bytes_estimate=160), 1_761_628_200.0
    )
    assert result == {
        "datasets": [
            {
                "exchange": "binance",
                "symbol": "ETHUSDT",
                "timeframe": "1h",
                "coverage": {
                    "first_ts": "2025-12-01T00:00:00+00:00",
                    "last_ts": "2026-01-31T23:00:00+00:00",
                },
                "rows": 0,
                "estimated_bytes": 0,
                "status": "ready",
                "last_used_at": "2026-10-01T00:00:00+00:00",
            },
            {
                "exchange": "binance",
                "symbol": "ETHUSDT",
                "timeframe": "1m",
                "coverage": {
                    "first_ts": "2025-12-01T00:00:00+00:00",
                    "last_ts": "2026-01-31T23:00:00+00:00",
                },
                "rows": 500,
                "estimated_bytes": 500 * 160,
                "status": "ready",
                "last_used_at": "2026-10-02T00:00:00+00:00",
            },
            {
                "exchange": "binance",
                "symbol": "BTCUSDT",
                "timeframe": "1m",
                "coverage": {
                    "first_ts": "2025-12-01T00:00:00+00:00",
                    "last_ts": "2026-01-31T23:00:00+00:00",
                },
                "rows": 300,
                "estimated_bytes": 300 * 160,
                "status": "ready",
                "last_used_at": "2026-10-03T00:00:00+00:00",
            },
        ],
        "totals": {
            "bytes": 800 * 160,
            "rows": 800,
            "pairs": 2,
            "budget_bytes": 536_870_912,
            "last_sweep": "2025-10-28T05:10:00+00:00",
        },
    }


async def test_a_rows_zero_dataset_renders_honestly_as_no_bars() -> None:
    """The rows=0 seed (an unfetchable pair's permanent row) is listed, not hidden."""
    conn = _InventoryConn(
        [_entry("ETHUSDT", "1h", 0, datetime(2026, 10, 1, tzinfo=UTC))],
        {"rows": 0, "datasets": 1},
    )
    result = await cache_inventory(_Pool(conn), DataConfig(), None)
    [dataset] = result["datasets"]
    assert dataset["rows"] == 0
    assert dataset["estimated_bytes"] == 0
    assert result["totals"]["last_sweep"] is None


async def test_an_empty_cache_lists_no_datasets() -> None:
    conn = _InventoryConn([], {"rows": 0, "datasets": 0})
    result = await cache_inventory(_Pool(conn), DataConfig(), None)
    assert result["datasets"] == []
    assert result["totals"] == {
        "bytes": 0,
        "rows": 0,
        "pairs": 0,
        "budget_bytes": 10 * 2**30,
        "last_sweep": None,
    }


async def test_the_inventory_without_a_pool_is_a_503() -> None:
    with pytest.raises(ServiceError) as err:
        await cache_inventory(None, DataConfig(), None)
    assert err.value.status_code == 503
    assert err.value.detail["code"] == "database_unavailable"
