"""The universe service: the system_config row read, refreshed and validated.

The repository seam (real SQL against real TimescaleDB) runs in the tests that boot the
real lifespan with the offline refresher factory; here the pool is a record stub.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from hydra.data.universe import SOURCE, UniverseEntry
from hydra.execution.client_factory import SharedRestClients
from hydra.services import universe as universe_svc
from hydra.services.errors import ServiceError
from hydra.services.universe import (
    default_compute,
    default_refresher,
    read_universe,
    refresh_universe,
    validate_symbol,
)

_SYMBOLS = [
    {"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1000.0},
    {"rank": 2, "symbol": "ETHUSDT", "quote_volume": 500.0},
    {"rank": 3, "symbol": "SOLUSDT", "quote_volume": 250.0},
]


def _value(ranked_at: datetime) -> dict[str, Any]:
    return {
        "exchange_id": "binance",
        "quote_asset": "USDT",
        "source": SOURCE,
        "ranked_at": ranked_at.isoformat(),
        "count": 3,
        "symbols": _SYMBOLS,
    }


class _UniverseConn:
    """Serves the ``universe`` row; an upsert lands in place, like ON CONFLICT."""

    def __init__(self, value: dict[str, Any] | None) -> None:
        self.row = None if value is None else {"value": json.dumps(value)}
        self.executed: list[tuple[Any, ...]] = []

    async def fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        assert "FROM system_config" in query
        assert args == ("universe",)
        return self.row

    async def execute(self, query: str, *args: Any) -> None:
        assert "system_config" in query
        self.executed.append((query, *args))
        self.row = {"value": args[1]}


class _Pool:
    def __init__(self, conn: _UniverseConn) -> None:
        self.conn = conn

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> _UniverseConn:
        return self.conn

    async def __aexit__(self, *exc: object) -> None:
        return None


async def _seeded(ranked_at: datetime) -> _Pool:
    return _Pool(_UniverseConn(_value(ranked_at)))


class TestReadUniverse:
    async def test_returns_the_stored_row_with_its_age(self) -> None:
        ranked_at = datetime.now(UTC) - timedelta(hours=1)
        result = await read_universe(await _seeded(ranked_at))
        assert result == {
            "exchange_id": "binance",
            "quote_asset": "USDT",
            "source": SOURCE,
            "ranked_at": ranked_at.isoformat(),
            "age_hours": pytest.approx(1.0, abs=0.01),
            "count": 3,
            "symbols": _SYMBOLS,
        }

    async def test_a_stale_row_is_served_as_stored(self) -> None:
        ranked_at = datetime.now(UTC) - timedelta(days=9)
        result = await read_universe(await _seeded(ranked_at))
        assert result["ranked_at"] == ranked_at.isoformat()
        assert result["age_hours"] == pytest.approx(9 * 24.0, abs=0.01)
        assert result["count"] == 3

    async def test_no_row_is_the_503_universe_unavailable_envelope(self) -> None:
        with pytest.raises(ServiceError) as err:
            await read_universe(_Pool(_UniverseConn(None)))
        assert err.value.status_code == 503
        assert err.value.detail == {
            "code": "universe_unavailable",
            "message": "The top-100 universe has not been computed yet",
            "state": "unavailable",
        }

    async def test_no_pool_is_the_503_database_envelope(self) -> None:
        with pytest.raises(ServiceError) as err:
            await read_universe(None)
        assert err.value.status_code == 503
        assert err.value.detail["code"] == "database_unavailable"


class TestValidateSymbol:
    async def test_a_member_passes(self) -> None:
        assert await validate_symbol(await _seeded(datetime.now(UTC)), "ETHUSDT") is None

    async def test_the_slash_form_normalizes_like_market_rules(self) -> None:
        assert await validate_symbol(await _seeded(datetime.now(UTC)), "BTC/USDT") is None

    async def test_the_lowercase_form_normalizes(self) -> None:
        assert await validate_symbol(await _seeded(datetime.now(UTC)), "solusdt") is None

    async def test_a_non_member_is_the_422_envelope_with_the_ranking_provenance(self) -> None:
        ranked_at = datetime.now(UTC) - timedelta(minutes=30)
        with pytest.raises(ServiceError) as err:
            await validate_symbol(await _seeded(ranked_at), "FOOBAR")
        assert err.value.status_code == 422
        assert err.value.detail == {
            "code": "symbol_not_in_universe",
            "message": (
                "FOOBAR is not in the current top-100 universe "
                f"(ranked at {ranked_at.isoformat()}); running sessions and stored "
                "strategies are unaffected"
            ),
            "state": "invalid",
            "symbol": "FOOBAR",
            "universe_ranked_at": ranked_at.isoformat(),
            "universe_count": 3,
        }

    async def test_no_row_fails_closed_with_the_503(self) -> None:
        with pytest.raises(ServiceError) as err:
            await validate_symbol(_Pool(_UniverseConn(None)), "BTCUSDT")
        assert err.value.status_code == 503
        assert err.value.detail["code"] == "universe_unavailable"


class TestRefreshUniverse:
    async def test_upserts_the_ranked_value_as_the_system_universe_row(self) -> None:
        conn = _UniverseConn(_value(datetime.now(UTC) - timedelta(days=2)))
        entries = [
            UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=100.0),
            UniverseEntry(rank=2, symbol="ETHUSDT", quote_volume=50.0),
        ]

        async def compute() -> list[UniverseEntry]:
            return entries

        await refresh_universe(_Pool(conn), compute)
        assert len(conn.executed) == 1
        query, *args = conn.executed[0]
        assert query == (
            "INSERT INTO system_config (key, value, updated_at, updated_by) "
            "VALUES ($1, $2::jsonb, now(), $3) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now(), "
            "updated_by = EXCLUDED.updated_by"
        )
        assert args[0] == "universe"
        assert args[2] == "system:universe"
        value = json.loads(args[1])
        assert value["exchange_id"] == "binance"
        assert value["quote_asset"] == "USDT"
        assert value["source"] == SOURCE
        assert value["count"] == 2
        assert value["symbols"] == [
            {"rank": 1, "symbol": "BTCUSDT", "quote_volume": 100.0},
            {"rank": 2, "symbol": "ETHUSDT", "quote_volume": 50.0},
        ]
        ranked_at = datetime.fromisoformat(value["ranked_at"])
        assert ranked_at.tzinfo is UTC
        assert (datetime.now(UTC) - ranked_at).total_seconds() < 5

    async def test_the_conflict_update_moves_ranked_at_in_place(self) -> None:
        old = datetime.now(UTC) - timedelta(days=2)
        pool = _Pool(_UniverseConn(_value(old)))

        async def compute() -> list[UniverseEntry]:
            return [UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=1.0)]

        await refresh_universe(pool, compute)
        result = await read_universe(pool)
        assert result["ranked_at"] != old.isoformat()
        assert result["age_hours"] < 0.01
        assert result["count"] == 1
        assert result["symbols"] == [{"rank": 1, "symbol": "BTCUSDT", "quote_volume": 1.0}]

    async def test_no_pool_is_the_503_database_envelope(self) -> None:
        async def compute() -> list[UniverseEntry]:
            return []

        with pytest.raises(ServiceError) as err:
            await refresh_universe(None, compute)
        assert err.value.status_code == 503
        assert err.value.detail["code"] == "database_unavailable"


class _FakeRestClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.closed = False

    async def load_markets(self) -> dict[str, Any]:
        self.calls.append("load_markets")
        return {
            "BTC/USDT": {
                "id": "BTCUSDT",
                "symbol": "BTC/USDT",
                "base": "BTC",
                "quote": "USDT",
                "spot": True,
                "active": True,
            }
        }

    async def fetch_tickers(self) -> dict[str, Any]:
        self.calls.append("fetch_tickers")
        return {"BTC/USDT": {"symbol": "BTC/USDT", "quoteVolume": 42.0}}

    async def close(self) -> None:
        self.closed = True


class TestTheProductionWiring:
    async def test_default_compute_rides_one_shared_rest_handle(self) -> None:
        clients: list[_FakeRestClient] = []

        def factory(exchange_id: str, creds: str | None) -> _FakeRestClient:
            assert (exchange_id, creds) == ("binance", None)
            client = _FakeRestClient()
            clients.append(client)
            return client

        compute = default_compute(SharedRestClients(rest_factory=factory))
        entries = await compute()
        assert entries == [UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=42.0)]
        assert len(clients) == 1
        assert clients[0].calls == ["load_markets", "fetch_tickers"]
        assert clients[0].closed  # the released last reference closed the client

    async def test_default_refresher_persists_the_pooled_compute(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = _UniverseConn(None)
        pool = _Pool(conn)

        async def fake_compute() -> list[UniverseEntry]:
            return [UniverseEntry(rank=1, symbol="BTCUSDT", quote_volume=100.0)]

        monkeypatch.setattr(universe_svc, "default_compute", lambda rest_pool=None: fake_compute)
        refresher = default_refresher(pool)
        await refresher.start()
        await asyncio.sleep(0)  # the immediate first refresh runs in the background
        await refresher.stop()
        assert len(conn.executed) == 1
        assert conn.executed[0][1] == "universe"
        assert json.loads(conn.executed[0][2])["count"] == 1
