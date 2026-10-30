"""Integration: the opt-in Redis fan-out against HYDRA_REDIS_URL (P7-E).

Real Redis, real sockets: cross-group fan-out (every group gets every event), the
one-group-per-role rule (consumers sharing a group load-balance — each event exactly
once across the group), a mid-session outage through a TCP proxy (delivered, dropped
+ counted, delivered again), redelivery dedup on ``event_id``, the dead-consumer
XAUTOCLAIM reclaim, and the slow-consumer pin (a hung consumer never backpressures
the publisher's mirror callback).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any
from urllib.parse import quote, urlparse, urlunparse

import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.event_bus import RedisEventPublisher, RedisEventSubscriber
from hydra.core.events import EntrySignal, Event
from hydra.core.types import Direction, MarketType, Symbol


def _entry(event_id: str) -> EntrySignal:
    return EntrySignal(
        event_id=event_id,
        symbol=Symbol("BTCUSDT"),
        direction=Direction.LONG,
        strength="0.7",
        strategy_id="it",
        exchange_id="binance",
        market_type=MarketType.SPOT,
    )


async def _until(condition: Callable[[], bool], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within the deadline")


async def _until_async(condition: Callable[[], Any], timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await condition():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met within the deadline")


def _drops(reason: str) -> float:
    value = REGISTRY.get_sample_value(
        "hydra_event_bus_redis_drops_total", {"event_type": "entry_signal", "reason": reason}
    )
    return value or 0.0


def _prefix() -> str:
    return f"hydra:it:{uuid.uuid4().hex[:8]}:"


async def _pending_empty(raw: Any, stream: str, group: str) -> bool:
    """True once the group's pending list is empty (every entry acked)."""
    try:
        pending = await raw.xpending(stream, group)
        count = pending.get("pending", 0) if isinstance(pending, dict) else pending[0]
        return int(count) == 0
    except Exception:
        return False


@pytest.fixture(autouse=True)
def _fresh_metrics() -> AsyncIterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


async def _subscriber(
    redis_url: str,
    prefix: str,
    group: str,
    received: list[str],
    *,
    consumer_name: str | None = None,
    **kwargs: Any,
) -> RedisEventSubscriber:
    subscriber = RedisEventSubscriber(
        redis_url,
        stream_prefix=prefix,
        group=group,
        consumer_name=consumer_name,
        **kwargs,
    )

    async def record(event: Event) -> None:
        received.append(event.event_id)

    await subscriber.connect()
    await subscriber.subscribe("entry_signal", record)
    return subscriber


class RedisProxy:
    """A TCP forwarder to Redis; ``stop()``/``start()`` are a mid-session outage.

    The publisher connects through the proxy while the subscriber stays directly
    connected, so an outage hits exactly the fan-out leg the mirror owns.
    """

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._server: asyncio.Server | None = None
        self._pumps: set[asyncio.Task[None]] = set()
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", self.port or 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        pumps, self._pumps = list(self._pumps), set()
        for pump in pumps:
            pump.cancel()
        for pump in pumps:
            with contextlib.suppress(asyncio.CancelledError):
                await pump
        self._server.close()
        await self._server.wait_closed()
        self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_reader, up_writer = await asyncio.open_connection(*self._target)

        async def pump(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
            while True:
                data = await source.read(4096)
                if not data:
                    break
                sink.write(data)
                await sink.drain()

        tasks = {
            asyncio.create_task(pump(reader, up_writer)),
            asyncio.create_task(pump(up_reader, writer)),
        }
        self._pumps.update(tasks)
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass
        finally:
            self._pumps.difference_update(tasks)
            writer.close()
            up_writer.close()


def _retarget(redis_url: str, host: str, port: int) -> str:
    """The Redis URL with host and port swapped for the proxy's (auth and db kept)."""
    parsed = urlparse(redis_url)
    netloc = f"{host}:{port}"
    if parsed.username or parsed.password:
        auth = parsed.username or ""
        if parsed.password:
            auth += f":{quote(parsed.password)}"
        netloc = f"{auth}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _host_port(redis_url: str) -> tuple[str, int]:
    parsed = urlparse(redis_url)
    return parsed.hostname or "127.0.0.1", parsed.port or 6379


@pytest.mark.integration
class TestFanOut:
    async def test_distinct_groups_each_receive_every_event(self, redis_url: str) -> None:
        """One group per role: both roles see all three events, in stream order."""
        prefix = _prefix()
        publisher = RedisEventPublisher(redis_url, stream_prefix=prefix)
        await publisher.connect()
        a: list[str] = []
        b: list[str] = []
        sub_a = await _subscriber(redis_url, prefix, "role-a", a)
        sub_b = await _subscriber(redis_url, prefix, "role-b", b)
        try:
            await sub_a.start_listening()
            await sub_b.start_listening()
            ids = ["e1", "e2", "e3"]
            for event_id in ids:
                await publisher.on_event(_entry(event_id))
            await _until(lambda: len(a) == 3 and len(b) == 3)
        finally:
            await sub_a.close()
            await sub_b.close()
            await publisher.close()
        assert a == ids
        assert b == ids

    async def test_a_shared_group_load_balances_each_event_exactly_once(
        self, redis_url: str
    ) -> None:
        """Two consumers sharing one group split the events; no event reaches both."""
        prefix = _prefix()
        publisher = RedisEventPublisher(redis_url, stream_prefix=prefix)
        await publisher.connect()
        a: list[str] = []
        b: list[str] = []
        sub_a = await _subscriber(redis_url, prefix, "shared", a, consumer_name="a")
        sub_b = await _subscriber(redis_url, prefix, "shared", b, consumer_name="b")
        try:
            await sub_a.start_listening()
            first = ["s1", "s2", "s3"]
            for event_id in first:
                await publisher.on_event(_entry(event_id))
            await _until(lambda: len(a) == 3)  # b is not listening yet: a owns them

            await sub_b.start_listening()
            second = ["s4", "s5", "s6"]
            for event_id in second:
                await publisher.on_event(_entry(event_id))
            await _until(lambda: len(a) + len(b) == 6)
        finally:
            await sub_a.close()
            await sub_b.close()
            await publisher.close()
        assert set(a) & set(b) == set()  # the group guarantee: exactly once
        assert set(a) | set(b) == {*first, *second}
        assert set(first) <= set(a)  # already delivered to a: never redelivered to b

    async def test_an_outage_mid_session_drops_and_counts_then_recovers(
        self, redis_url: str
    ) -> None:
        """3 delivered, 5 dropped + counted through the outage, 2 more after recovery."""
        host, port = _host_port(redis_url)
        proxy = RedisProxy(host, port)
        await proxy.start()
        prefix = _prefix()
        publisher = RedisEventPublisher(
            _retarget(redis_url, "127.0.0.1", proxy.port), stream_prefix=prefix
        )
        await publisher.connect()
        received: list[str] = []
        subscriber = await _subscriber(redis_url, prefix, "outage", received)
        try:
            await subscriber.start_listening()
            for event_id in ("up1", "up2", "up3"):
                await publisher.on_event(_entry(event_id))
            await _until(lambda: len(received) == 3)

            await proxy.stop()
            for event_id in ("down1", "down2", "down3", "down4", "down5"):
                await publisher.on_event(_entry(event_id))
            await _until(lambda: _drops("publish_failed") == 5)

            await proxy.start()  # same port: the pool reconnects through it
            for event_id in ("back1", "back2"):
                await publisher.on_event(_entry(event_id))
            await _until(lambda: len(received) == 5)
        finally:
            await subscriber.close()
            await publisher.close()
            if proxy._server is not None:
                await proxy.stop()
        assert received == ["up1", "up2", "up3", "back1", "back2"]
        assert _drops("publish_failed") == 5.0
        assert _drops("buffer_full") == 0.0

    async def test_a_redelivered_event_id_is_processed_once(self, redis_url: str) -> None:
        """Two stream entries carrying one event_id (the XAUTOCLAIM-redelivery shape):
        the callback runs once, both entries are acked."""
        import redis.asyncio as aioredis

        prefix = _prefix()
        publisher = RedisEventPublisher(redis_url, stream_prefix=prefix)
        await publisher.connect()
        received: list[str] = []
        subscriber = await _subscriber(redis_url, prefix, "dedup", received)
        stream = f"{prefix}entry_signal"
        raw = aioredis.from_url(redis_url, decode_responses=False)
        try:
            await subscriber.start_listening()
            duplicate = _entry("dup")
            await publisher.on_event(duplicate)
            await publisher.on_event(duplicate)  # a second entry, the same event
            await _until(lambda: len(received) == 1)
            await _until_async(lambda: _pending_empty(raw, stream, "dedup"))
        finally:
            await raw.aclose()
            await subscriber.close()
            await publisher.close()
        assert received == ["dup"]

    async def test_a_slow_consumer_never_backpressures_the_mirror(self, redis_url: str) -> None:
        """30 mirrors against a 200 ms-per-event consumer complete in under 50 ms."""
        prefix = _prefix()
        publisher = RedisEventPublisher(redis_url, stream_prefix=prefix)
        await publisher.connect()
        processed: list[str] = []

        async def slow(event: Event) -> None:
            await asyncio.sleep(0.2)
            processed.append(event.event_id)

        subscriber = RedisEventSubscriber(redis_url, stream_prefix=prefix, group="slow")
        await subscriber.connect()
        await subscriber.subscribe("entry_signal", slow)
        await subscriber.start_listening()
        try:
            started = time.perf_counter()
            for i in range(30):
                await publisher.on_event(_entry(f"slow-{i}"))
            elapsed = time.perf_counter() - started
            assert elapsed < 0.05, f"30 mirrors took {elapsed * 1000:.1f} ms"
            await _until(lambda: len(processed) == 30, timeout=60.0)
        finally:
            await subscriber.close()
            await publisher.close()
        assert len(set(processed)) == 30

    async def test_a_dead_consumers_entries_are_reclaimed(self, redis_url: str) -> None:
        """XAUTOCLAIM takes over entries a consumer read but never acked."""
        import redis.asyncio as aioredis

        prefix = _prefix()
        publisher = RedisEventPublisher(redis_url, stream_prefix=prefix)
        await publisher.connect()
        stream = f"{prefix}entry_signal"
        raw = aioredis.from_url(redis_url, decode_responses=False)
        try:
            await raw.xgroup_create(stream, "reclaim-group", id="0", mkstream=True)
            for event_id in ("d1", "d2", "d3"):
                await publisher.on_event(_entry(event_id))
            # the dead consumer reads all three and never acks; the drain flushes in
            # batches, so keep reading until it holds every entry
            deadline = time.monotonic() + 10.0
            dead = 0
            while time.monotonic() < deadline and dead < 3:
                result = await raw.xreadgroup(
                    "reclaim-group", "dead", {stream: ">"}, count=10, block=500
                )
                dead += len(result[0][1]) if result else 0
            assert dead == 3
        finally:
            await raw.aclose()

        await asyncio.sleep(0.3)  # past the reclaim threshold below
        received: list[str] = []
        subscriber = await _subscriber(
            redis_url,
            prefix,
            "reclaim-group",
            received,
            consumer_name="alive",
            min_idle_time_ms=100,
        )
        try:
            await subscriber.start_listening()
            await _until(lambda: len(received) == 3)
        finally:
            await subscriber.close()
            await publisher.close()
        assert sorted(received) == ["d1", "d2", "d3"]
        reclaimed = REGISTRY.get_sample_value("hydra_event_bus_redis_reclaimed_total")
        assert reclaimed == 3.0


async def _pending_empty(raw: Any, stream: str, group: str) -> bool:
    """True once the group's pending list is empty (every entry acked)."""
    try:
        pending = await raw.xpending(stream, group)
        count = pending.get("pending", 0) if isinstance(pending, dict) else pending[0]
        return int(count) == 0
    except Exception:
        return False
