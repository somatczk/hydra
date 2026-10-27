"""SharedRestClients (S10b): one refcounted REST client per (exchange, credential) key.

The pool and the feed/session wiring are real; only the ccxt boundary is faked. Every
release path is pinned: last release closes, double release is a hard error, a close
failure never poisons the releaser, and a warm-up that fails mid-startup releases its
handle. Two feeds (and two sessions) share exactly one public client per exchange.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.types import Timeframe
from hydra.data.ingestion import ExchangeFeedManager, InsufficientHistoryError
from hydra.execution.client_factory import ClientFactory, SharedRestClients
from hydra.execution.errors import UnsupportedVenueError
from hydra.execution.session_manager import SessionManager
from hydra.risk.state import InMemoryRiskStateStore
from tests.fixtures.binance_kline import FakeRestExchange, FakeWsExchange
from tests.unit.test_execution.session_harness import history_rows, write_strategy


def _rows() -> list[list[Any]]:
    return history_rows(datetime.now(UTC), 20)


class RecordingFactory:
    """Pool factory stand-in: records ``(exchange_id, creds)`` per construction."""

    def __init__(self, rows: list[list[Any]] | None = None, fail_markets: bool = False) -> None:
        self.rows = rows if rows is not None else []
        self.fail_markets = fail_markets
        self.calls: list[tuple[str, str | None]] = []
        self.clients: list[FakeRestExchange] = []

    def __call__(self, exchange_id: str, creds: str | None) -> FakeRestExchange:
        self.calls.append((exchange_id, creds))
        client = _RecordingRest(self.rows, self.fail_markets)
        self.clients.append(client)
        return client


class _RecordingRest(FakeRestExchange):
    """A ``FakeRestExchange`` that can fail ``load_markets`` and record ``close`` errors."""

    def __init__(self, rows: list[list[Any]], fail_markets: bool) -> None:
        super().__init__(rows)
        self.fail_markets = fail_markets
        self.close_error: BaseException | None = None

    async def load_markets(self) -> dict[str, Any]:
        if self.fail_markets:
            raise ConnectionError("load_markets failed")
        return await super().load_markets()

    async def close(self) -> None:
        await super().close()
        if self.close_error is not None:
            raise self.close_error


def _feed(pool: SharedRestClients) -> ExchangeFeedManager:
    return ExchangeFeedManager(
        event_bus=InMemoryEventBus(), rest_pool=pool, repository=None, history_window=20
    )


def _clients(factory: RecordingFactory) -> ClientFactory:
    return ClientFactory(
        ws_factory=lambda _eid: lambda: FakeWsExchange(),
        rest_pool=SharedRestClients(rest_factory=factory),
        repository_factory=lambda _pool: None,
        history_window=20,
    )


def _manager(clients: ClientFactory) -> SessionManager:
    return SessionManager(
        db_pool=None,
        config=HydraConfig(),
        clients=clients,
        risk_store=InMemoryRiskStateStore(paper_capital=Decimal("1000")),
    )


# ----------------------------------------------------------------------
# Refcount lifecycle
# ----------------------------------------------------------------------


async def test_first_reference_constructs_and_last_release_closes() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    handle = await pool.handle("binance")
    assert factory.calls == [("binance", None)]
    assert handle.client is factory.clients[0]
    assert factory.clients[0].close_calls == 0

    await handle.release()
    assert factory.clients[0].close_calls == 1


async def test_second_reference_reuses_the_client_and_releases_in_order() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    first = await pool.handle("binance")
    second = await pool.handle("binance")
    assert len(factory.calls) == 1
    assert second.client is first.client

    await first.release()
    assert factory.clients[0].close_calls == 0  # one reference still held
    await second.release()
    assert factory.clients[0].close_calls == 1


async def test_concurrent_acquires_construct_exactly_one_client() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    handles = await asyncio.gather(*[pool.handle("binance") for _ in range(8)])

    assert len(factory.calls) == 1
    assert {h.client for h in handles} == {factory.clients[0]}
    for handle in handles[:-1]:
        await handle.release()
    assert factory.clients[0].close_calls == 0
    await handles[-1].release()
    assert factory.clients[0].close_calls == 1


async def test_reacquire_after_close_reconstructs() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    first = await pool.handle("binance")
    await first.release()
    second = await pool.handle("binance")

    assert len(factory.calls) == 2
    assert second.client is not first.client
    assert factory.clients[0].close_calls == 1

    await second.release()
    assert factory.clients[1].close_calls == 1


async def test_double_release_is_a_hard_error() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    handle = await pool.handle("binance")
    await handle.release()
    with pytest.raises(RuntimeError, match=r"release\(\) called twice"):
        await handle.release()

    # The refused double release did not over-decrement a live entry.
    fresh = await pool.handle("binance")
    assert fresh.client is factory.clients[1]
    await fresh.release()
    assert factory.clients[1].close_calls == 1


# ----------------------------------------------------------------------
# Keying: the public entry and credential entries coexist
# ----------------------------------------------------------------------


async def test_public_and_credential_keys_are_distinct_entries() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    public = await pool.handle("binance")
    keyed = await pool.handle("binance", creds="cred-1")

    assert factory.calls == [("binance", None), ("binance", "cred-1")]
    assert keyed.client is not public.client

    await public.release()
    assert factory.clients[0].close_calls == 1
    assert factory.clients[1].close_calls == 0  # the credential entry outlives the public one

    await keyed.release()
    assert factory.clients[1].close_calls == 1


async def test_credential_identities_key_separately() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    one = await pool.handle("binance", creds="cred-1")
    two = await pool.handle("binance", creds="cred-2")
    assert factory.calls == [("binance", "cred-1"), ("binance", "cred-2")]
    assert one.client is not two.client
    await one.release()
    await two.release()
    assert factory.clients[0].close_calls == 1
    assert factory.clients[1].close_calls == 1


async def test_default_factory_refuses_credential_keys_until_p3_a() -> None:
    pool = SharedRestClients()
    with pytest.raises(NotImplementedError, match="P3-A"):
        await pool.handle("binance", creds="cred-1")


async def test_default_pool_validates_the_venue_verbatim() -> None:
    pool = SharedRestClients()
    with pytest.raises(UnsupportedVenueError) as err:
        await pool.handle("kraken")
    assert err.value.code == "unsupported_venue"


# ----------------------------------------------------------------------
# Failure behaviour
# ----------------------------------------------------------------------


async def test_a_close_failure_never_poisons_the_releaser() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    handle = await pool.handle("binance")
    factory.clients[0].close_error = RuntimeError("close failed")
    await handle.release()  # must not raise

    assert factory.clients[0].close_calls == 1
    fresh = await pool.handle("binance")
    assert fresh.client is factory.clients[1]


async def test_markets_loader_fetches_through_a_pooled_handle_and_releases_it() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)

    markets = await pool.markets_loader("binance")

    assert [m["symbol"] for m in markets] == [
        "BTC/USDT",
        "BTC/USDT:USDT",
        "ETH/USDT",
    ]
    assert len(factory.calls) == 1
    assert factory.clients[0].close_calls == 1  # the acquire was balanced by the release


async def test_markets_loader_releases_when_load_markets_raises() -> None:
    factory = RecordingFactory(_rows(), fail_markets=True)
    pool = SharedRestClients(rest_factory=factory)

    with pytest.raises(ConnectionError, match="load_markets failed"):
        await pool.markets_loader("binance")

    assert factory.clients[0].close_calls == 1
    factory.fail_markets = False
    assert len(await pool.markets_loader("binance")) == 3  # the pool is not poisoned


# ----------------------------------------------------------------------
# The feed seam: acquire on first REST use, release on disconnect
# ----------------------------------------------------------------------


async def test_two_feeds_share_one_client_until_the_last_disconnect() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)
    one = _feed(pool)
    two = _feed(pool)

    await one.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=10)
    await two.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=10)

    assert len(factory.calls) == 1  # exactly one public REST client for the exchange
    shared = factory.clients[0]
    assert one._rest_handles["binance"].client is shared
    assert two._rest_handles["binance"].client is shared

    await one.disconnect_all()
    assert shared.close_calls == 0  # the second feed still holds the reference
    await two.disconnect_all()
    assert shared.close_calls == 1


async def test_concurrent_cold_fetches_keep_the_refcount_balanced() -> None:
    factory = RecordingFactory(_rows())
    pool = SharedRestClients(rest_factory=factory)
    feed = _feed(pool)

    async def cold_fetch() -> None:
        client, _markets = await feed._ensure_rest("binance")
        await client.fetch_ohlcv("BTC/USDT", "1h", limit=1)

    await asyncio.gather(cold_fetch(), cold_fetch(), cold_fetch())

    assert len(factory.calls) == 1
    assert len(feed._rest_handles) == 1

    await feed.disconnect_all()
    assert factory.clients[0].close_calls == 1


async def test_feed_warm_up_failure_releases_its_handle() -> None:
    factory = RecordingFactory([])  # no history stored, none fetchable
    pool = SharedRestClients(rest_factory=factory)
    feed = _feed(pool)

    with pytest.raises(InsufficientHistoryError):
        await feed.warm_up(["BTCUSDT"], [Timeframe.H1], min_bars=20)

    client = feed._rest_handles["binance"].client
    await feed.disconnect_all()
    assert client.close_calls == 1
    assert not feed._rest_handles


# ----------------------------------------------------------------------
# The session seam: ClientFactory.rest_pool plumbing
# ----------------------------------------------------------------------


async def test_running_sessions_share_one_pooled_client_and_stops_leave_zero_refs(
    strategies_dir: Path,
) -> None:
    write_strategy(strategies_dir, "pool-a")
    write_strategy(strategies_dir, "pool-b")
    factory = RecordingFactory(_rows())
    mgr = _manager(_clients(factory))
    await mgr.startup()

    sid_a = await mgr.start_session("pool-a", "paper", Decimal("500"))
    sid_b = await mgr.start_session("pool-b", "paper", Decimal("500"))

    # One transient client for the market-rules loader (the second start hits the
    # rules cache), one shared client behind both sessions' feeds — nothing more.
    assert factory.calls == [("binance", None), ("binance", None)]
    shared = factory.clients[1]
    assert mgr.get_session(sid_a)._runtime.feed._rest_handles["binance"].client is shared
    assert mgr.get_session(sid_b)._runtime.feed._rest_handles["binance"].client is shared

    await mgr.stop_session(sid_a)
    assert shared.close_calls == 0  # session B's feed still holds the reference
    await mgr.stop_session(sid_b)
    assert shared.close_calls == 1
    await mgr.graceful_shutdown()

    assert all(client.close_calls == 1 for client in factory.clients)


async def test_session_start_failure_leaves_no_pooled_references(strategies_dir: Path) -> None:
    write_strategy(strategies_dir, "pool-empty")
    factory = RecordingFactory([])  # warm-up can never gather history
    mgr = _manager(_clients(factory))
    await mgr.startup()

    with pytest.raises(InsufficientHistoryError):
        await mgr.start_session("pool-empty", "paper")

    await mgr.graceful_shutdown()
    # Every construction — the loader's transient client and the failed feed's — is closed.
    assert factory.calls == [("binance", None), ("binance", None)]
    assert all(client.close_calls == 1 for client in factory.clients)
