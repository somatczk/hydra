"""Event bus implementations: the in-memory trading bus and the opt-in Redis fan-out.

Every session runs an :class:`InMemoryEventBus` (deterministic, subscriber-isolated) —
it implements the ``EventBus`` protocol from ``hydra.core.protocols`` and is the only
bus the trading path ever reads. :class:`RedisEventPublisher` mirrors each session's
events to Redis Streams when ``events.bus: redis`` is set (publish-only fan-out:
serialize + enqueue, a background XADD drain, drops counted, never a raise into the
bar path), and :class:`RedisEventSubscriber` is the consumer-side API for
cross-process readers. Trading consumers never read from Redis.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import OrderedDict, defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import msgpack

from hydra.core import metrics
from hydra.core.events import Event, event_from_dict, event_to_dict

logger = logging.getLogger(__name__)

type EventCallback = Callable[[Event], Awaitable[None]]


# ---------------------------------------------------------------------------
# InMemoryEventBus — deterministic, for backtesting
# ---------------------------------------------------------------------------


class InMemoryEventBus:
    """Async in-memory event bus with deterministic timestamp ordering.

    Suitable for backtesting where events must be replayed in exact order.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[EventCallback]] = defaultdict(list)
        self._queue: asyncio.PriorityQueue[tuple[float, str, Event]] = asyncio.PriorityQueue()
        self._running: bool = False
        self._counter: int = 0  # tiebreaker for same-timestamp events
        self.handler_errors: int = 0

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers matching its event_type.

        Events are dispatched immediately (synchronously from the caller's perspective) so
        that the backtest loop stays deterministic. Each callback is isolated: an exception is
        logged and counted, and later subscribers still receive the event. Cancellation
        propagates.
        """
        event_type = event.event_type
        callbacks = list(self._subscribers.get(event_type, []))
        # Also notify wildcard subscribers
        callbacks.extend(self._subscribers.get("*", []))
        t0 = time.monotonic()
        for callback in callbacks:
            try:
                await callback(event)
            except Exception:
                self.handler_errors += 1
                logger.exception(
                    "Event handler %s failed for %s",
                    getattr(callback, "__qualname__", repr(callback)),
                    event_type,
                )
                metrics.record_event_handler_error(event_type)
        metrics.observe_event_bus_latency(time.monotonic() - t0)

    async def publish_queued(self, event: Event) -> None:
        """Enqueue an event for later ordered processing via ``drain``."""
        self._counter += 1
        self._queue.put_nowait((event.timestamp.timestamp(), str(self._counter).zfill(20), event))

    async def drain(self) -> None:
        """Process all queued events in timestamp order."""
        while not self._queue.empty():
            _, _, event = self._queue.get_nowait()
            await self.publish(event)

    async def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Subscribe a callback to events of the given type.

        Use ``"*"`` to subscribe to all event types.
        """
        if callback not in self._subscribers[event_type]:
            self._subscribers[event_type].append(callback)

    async def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        """Remove a callback subscription."""
        with contextlib.suppress(ValueError):
            self._subscribers[event_type].remove(callback)

    def subscriber_count(self, event_type: str) -> int:
        """Return number of subscribers for a given event type."""
        return len(self._subscribers.get(event_type, []))

    def clear(self) -> None:
        """Remove all subscribers and queued events."""
        self._subscribers.clear()
        # Drain the priority queue
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break


# ---------------------------------------------------------------------------
# The opt-in Redis Streams fan-out (P7-E): a publish-only mirror, plus a
# consumer API for cross-process readers. Trading consumers never read these
# streams; every session keeps its InMemoryEventBus as the trading bus.
# ---------------------------------------------------------------------------

_RETRY_MAX_DELAY_S = 5.0


async def require_redis(redis_url: str, *, budget_s: float = 30.0) -> None:
    """PING Redis, retrying while it is unreachable; the last error raises past the budget.

    The startup fail-fast for ``events.bus: redis`` (the dashboard lifespan and the
    trader process both call this): with the fan-out on, Redis is tier-1 and a process
    that cannot reach it refuses to start, exactly like the database.
    """
    import redis.asyncio as aioredis

    client: Any = aioredis.from_url(redis_url, decode_responses=False)
    try:
        await _ping_with_retry(client, budget_s=budget_s)
    finally:
        await client.aclose()


async def _ping_with_retry(client: Any, *, budget_s: float) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_s
    delay = 0.5
    while True:
        try:
            await client.ping()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if loop.time() + delay > deadline:
                raise
            logger.warning(
                "Redis not reachable yet (%s); retrying in %.1fs", type(exc).__name__, delay
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_DELAY_S)


class RedisEventPublisher:
    """Mirrors every event of one session's in-memory bus to Redis Streams.

    Subscribed as the session bus's last ``'*'`` callback, so the existing isolation
    guarantee covers Redis failures for free: ``on_event`` only serializes and
    enqueues (``put_nowait`` — never an await on the wire, never a raise), and a
    background task drains the bounded queue in pipelined XADD batches. A full
    buffer or a failed XADD drops the event and counts it; the bar path is never
    blocked and the drops are visible in ``hydra_event_bus_redis_drops_total``.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        stream_prefix: str = "hydra:events:",
        maxlen: int = 10_000,
        buffer_size: int = 8_192,
        batch_size: int = 256,
        retry_budget_s: float = 30.0,
        client: Any = None,
    ) -> None:
        self._redis_url = redis_url
        self._stream_prefix = stream_prefix
        self._maxlen = maxlen
        self._batch_size = batch_size
        self._retry_budget_s = retry_budget_s
        self._queue: asyncio.Queue[tuple[str, bytes]] = asyncio.Queue(maxsize=buffer_size)
        self._client = client
        self._drain_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        """PING with a retry budget, then start the drain loop (fail-fast for callers)."""
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url, decode_responses=False)
        await _ping_with_retry(self._client, budget_s=self._retry_budget_s)
        if self._drain_task is None or self._drain_task.done():
            self._drain_task = asyncio.create_task(self._drain_loop(), name="redis-event-fanout")

    async def on_event(self, event: Event) -> None:
        """The mirror callback: serialize and enqueue; never awaits the wire, never raises."""
        try:
            payload = msgpack.packb(event_to_dict(event), use_bin_type=True)
        except Exception:
            metrics.record_event_bus_redis_drop(event.event_type, "publish_failed")
            return
        try:
            self._queue.put_nowait((event.event_type, payload))
        except asyncio.QueueFull:
            metrics.record_event_bus_redis_drop(event.event_type, "buffer_full")

    async def close(self) -> None:
        """Cancel the drain loop and close the connection; buffered events are dropped."""
        task, self._drain_task = self._drain_task, None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    async def _drain_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._batch_size and not self._queue.empty():
                batch.append(self._queue.get_nowait())
            await self._xadd_batch(batch)

    async def _xadd_batch(self, batch: list[tuple[str, bytes]]) -> None:
        client = self._client
        if client is None:
            return
        try:
            async with client.pipeline(transaction=False) as pipe:
                for event_type, payload in batch:
                    pipe.xadd(
                        f"{self._stream_prefix}{event_type}",
                        {"data": payload},
                        maxlen=self._maxlen,
                        approximate=True,
                    )
                await pipe.execute()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Redis event fan-out XADD failed; dropped %d events", len(batch))
            for event_type, _payload in batch:
                metrics.record_event_bus_redis_drop(event_type, "publish_failed")


class RedisEventSubscriber:
    """Consumes the mirrored events from Redis Streams, for cross-process readers only.

    One group per role: every consumer group receives every event, while consumers
    sharing one group load-balance (each event goes to exactly one of them) — pick a
    group name that names the consumer role, never share a group by accident. Each
    stream is read with ``XREADGROUP`` and reclaimed from dead consumers with
    ``XAUTOCLAIM`` every iteration; a bounded ``event_id`` LRU skips redeliveries,
    callbacks are isolated (a raising callback is logged and counted, never
    propagated) and the ``XACK`` lands after they run.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        *,
        stream_prefix: str = "hydra:events:",
        group: str = "hydra",
        consumer_name: str | None = None,
        block_ms: int = 1000,
        count: int = 10,
        min_idle_time_ms: int = 60_000,
        dedup_size: int = 4096,
        client: Any = None,
    ) -> None:
        self._redis_url = redis_url
        self._stream_prefix = stream_prefix
        self._group = group
        self._consumer_name = consumer_name or f"consumer-{id(self)}"
        self._block_ms = block_ms
        self._count = count
        self._min_idle_time_ms = min_idle_time_ms
        self._client = client
        self._subscribers: dict[str, list[EventCallback]] = defaultdict(list)
        self._ensured_groups: set[str] = set()
        self._listen_tasks: dict[str, asyncio.Task[None]] = {}
        self._running = False
        self._recent: OrderedDict[str, None] = OrderedDict()
        self._dedup_size = dedup_size

    # -- Connection management -----------------------------------------------

    async def connect(self) -> None:
        """Establish and verify the connection to Redis."""
        if self._client is None:
            import redis.asyncio as aioredis

            self._client = aioredis.from_url(self._redis_url, decode_responses=False)
        await self._client.ping()

    async def close(self) -> None:
        """Stop listening and close the connection."""
        await self.stop_listening()
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()

    # -- Subscription management ----------------------------------------------

    async def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Subscribe a callback to one event type's stream ('*' is not a stream key)."""
        if event_type == "*":
            raise ValueError(
                "subscribe to concrete event types: the mirror's streams are keyed by event type"
            )
        if callback not in self._subscribers[event_type]:
            self._subscribers[event_type].append(callback)
        await self._ensure_group(event_type)
        if self._running and event_type not in self._listen_tasks:
            self._listen_tasks[event_type] = asyncio.create_task(
                self._listen_loop(event_type), name=f"redis-event-sub-{event_type}"
            )

    async def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        """Remove a subscription."""
        with contextlib.suppress(ValueError):
            self._subscribers[event_type].remove(callback)

    async def start_listening(self) -> None:
        """Start one background reader per subscribed stream."""
        self._running = True
        for event_type in list(self._subscribers):
            if event_type not in self._listen_tasks:
                self._listen_tasks[event_type] = asyncio.create_task(
                    self._listen_loop(event_type), name=f"redis-event-sub-{event_type}"
                )

    async def stop_listening(self) -> None:
        """Stop all background reader tasks."""
        self._running = False
        tasks, self._listen_tasks = list(self._listen_tasks.values()), {}
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    # -- Reading ---------------------------------------------------------------

    async def _ensure_group(self, event_type: str) -> None:
        """Create the stream and consumer group once per stream; BUSYGROUP is fine."""
        stream_key = self._stream_key(event_type)
        if stream_key in self._ensured_groups:
            return
        try:
            await self._client.xgroup_create(stream_key, self._group, id="0", mkstream=True)
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._ensured_groups.add(stream_key)

    def _stream_key(self, event_type: str) -> str:
        return f"{self._stream_prefix}{event_type}"

    async def _listen_loop(self, event_type: str) -> None:
        """Read new entries, then reclaim abandoned ones, forever until stopped."""
        stream_key = self._stream_key(event_type)
        while self._running:
            try:
                results = await self._client.xreadgroup(
                    groupname=self._group,
                    consumername=self._consumer_name,
                    streams={stream_key: ">"},
                    count=self._count,
                    block=self._block_ms,
                )
                for _stream, messages in results or []:
                    await self._deliver(stream_key, event_type, messages)
                await self._reclaim(stream_key, event_type)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Redis event subscriber loop for %s failed; retrying", event_type)
                await asyncio.sleep(1)

    async def _deliver(
        self, stream_key: str, event_type: str, messages: list[tuple[bytes, dict[bytes, bytes]]]
    ) -> None:
        """Run the callbacks for each entry (deduped), then ack it — also when it is a
        poison payload, so an undecodable entry cannot be reclaimed forever."""
        for msg_id, msg_data in messages:
            event: Event | None = None
            try:
                payload = msgpack.unpackb(msg_data[b"data"], raw=False)
                event = event_from_dict(payload)
            except Exception:
                logger.exception("Undecodable stream entry %s on %s", msg_id, stream_key)
            if event is not None and not self._seen(event.event_id):
                for callback in list(self._subscribers.get(event_type, [])):
                    try:
                        await callback(event)
                    except Exception:
                        logger.exception("Event callback failed for %s", event_type)
                        metrics.record_event_handler_error(event_type)
            try:
                await self._client.xack(stream_key, self._group, msg_id)
            except Exception:
                logger.exception("XACK failed for %s; a redelivery is deduped", msg_id)

    async def _reclaim(self, stream_key: str, event_type: str) -> None:
        """Claim entries idle past ``min_idle_time_ms`` from consumers that never acked."""
        try:
            result = await self._client.xautoclaim(
                stream_key,
                self._group,
                self._consumer_name,
                min_idle_time=self._min_idle_time_ms,
                start_id="0-0",
                count=self._count,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("XAUTOCLAIM failed on %s", stream_key)
            return
        messages = result[1] if isinstance(result, (list, tuple)) and len(result) > 1 else []
        if not messages:
            return
        metrics.record_event_bus_redis_reclaimed(len(messages))
        await self._deliver(stream_key, event_type, messages)

    def _seen(self, event_id: str) -> bool:
        """True for a redelivery; records the id either way, bounded by ``dedup_size``."""
        if event_id in self._recent:
            self._recent.move_to_end(event_id)
            return True
        self._recent[event_id] = None
        if len(self._recent) > self._dedup_size:
            self._recent.popitem(last=False)
        return False
