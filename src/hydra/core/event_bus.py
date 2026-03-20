"""Event bus implementations: in-memory (backtest) and Redis Streams (live).

Both implement the ``EventBus`` protocol from ``hydra.core.protocols``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any

import msgpack

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

    async def publish(self, event: Event) -> None:
        """Publish an event to all subscribers matching its event_type.

        In backtest mode events are dispatched immediately (synchronously from
        the caller's perspective) so that the backtest loop stays deterministic.
        """
        event_type = event.event_type
        callbacks = list(self._subscribers.get(event_type, []))
        # Also notify wildcard subscribers
        callbacks.extend(self._subscribers.get("*", []))
        t0 = time.monotonic()
        for callback in callbacks:
            await callback(event)
        try:
            from hydra.dashboard.metrics import observe_event_bus_latency

            observe_event_bus_latency(time.monotonic() - t0)
        except Exception:
            pass

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
# RedisEventBus — production, Redis Streams with consumer groups
# ---------------------------------------------------------------------------


class RedisEventBus:
    """Redis Streams event bus with consumer groups and msgpack serialization.

    Supports pattern-based subscription, automatic stream/group creation,
    MAXLEN trimming, and reconnection logic.
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379",
        stream_prefix: str = "hydra:events:",
        consumer_group: str = "hydra",
        consumer_name: str | None = None,
        maxlen: int = 10_000,
    ) -> None:
        self._redis_url = redis_url
        self._stream_prefix = stream_prefix
        self._consumer_group = consumer_group
        self._consumer_name = consumer_name or f"consumer-{id(self)}"
        self._maxlen = maxlen

        self._redis: Any = None
        self._subscribers: dict[str, list[EventCallback]] = defaultdict(list)
        self._running: bool = False
        self._listen_tasks: dict[str, asyncio.Task[None]] = {}

    # -- Connection management -----------------------------------------------

    async def connect(self) -> None:
        """Establish connection to Redis."""
        import redis.asyncio as aioredis

        self._redis = aioredis.from_url(
            self._redis_url,
            decode_responses=False,
        )
        # Verify connection
        await self._redis.ping()

    async def disconnect(self) -> None:
        """Close the Redis connection."""
        self._running = False
        for task in self._listen_tasks.values():
            task.cancel()
        self._listen_tasks.clear()
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None

    async def _ensure_connected(self) -> None:
        """Reconnect if the connection has been lost."""
        if self._redis is None:
            await self.connect()
        else:
            try:
                await self._redis.ping()
            except Exception:
                logger.warning("Redis connection lost, reconnecting...")
                await self.connect()

    # -- Stream management ---------------------------------------------------

    def _stream_key(self, event_type: str) -> str:
        return f"{self._stream_prefix}{event_type}"

    async def _ensure_stream_group(self, stream_key: str) -> None:
        """Create the stream and consumer group if they don't exist."""
        try:
            await self._redis.xgroup_create(
                stream_key,
                self._consumer_group,
                id="0",
                mkstream=True,
            )
        except Exception as exc:
            # BUSYGROUP means the group already exists
            if "BUSYGROUP" not in str(exc):
                raise

    # -- Publish / Subscribe -------------------------------------------------

    async def publish(self, event: Event) -> None:
        """Publish an event to a Redis Stream, msgpack-serialized."""
        await self._ensure_connected()
        stream_key = self._stream_key(event.event_type)
        await self._ensure_stream_group(stream_key)

        payload = msgpack.packb(event_to_dict(event), use_bin_type=True)
        await self._redis.xadd(
            stream_key,
            {"data": payload},
            maxlen=self._maxlen,
            approximate=True,
        )

        # Also dispatch locally to in-process subscribers
        event_type = event.event_type
        callbacks = list(self._subscribers.get(event_type, []))
        callbacks.extend(self._subscribers.get("*", []))
        for callback in callbacks:
            try:
                await callback(event)
            except Exception:
                logger.exception("Error in event callback for %s", event_type)

    async def subscribe(self, event_type: str, callback: EventCallback) -> None:
        """Subscribe to events of a given type (or ``'*'`` for all)."""
        if callback not in self._subscribers[event_type]:
            self._subscribers[event_type].append(callback)

        # Ensure stream exists for non-wildcard subscriptions
        if event_type != "*":
            await self._ensure_connected()
            stream_key = self._stream_key(event_type)
            await self._ensure_stream_group(stream_key)

    async def unsubscribe(self, event_type: str, callback: EventCallback) -> None:
        """Remove a subscription."""
        with contextlib.suppress(ValueError):
            self._subscribers[event_type].remove(callback)

    async def start_listening(self) -> None:
        """Start background tasks that read from Redis Streams."""
        self._running = True
        for event_type in list(self._subscribers.keys()):
            if event_type == "*":
                continue
            if event_type not in self._listen_tasks:
                task = asyncio.create_task(self._listen_loop(event_type))
                self._listen_tasks[event_type] = task

    async def _listen_loop(self, event_type: str) -> None:
        """Continuously read events from a Redis Stream consumer group."""
        stream_key = self._stream_key(event_type)
        while self._running:
            try:
                await self._ensure_connected()
                results = await self._redis.xreadgroup(
                    groupname=self._consumer_group,
                    consumername=self._consumer_name,
                    streams={stream_key: ">"},
                    count=10,
                    block=1000,
                )
                if not results:
                    continue
                for _stream, messages in results:
                    for msg_id, msg_data in messages:
                        try:
                            payload = msgpack.unpackb(msg_data[b"data"], raw=False)
                            event = event_from_dict(payload)
                            callbacks = list(self._subscribers.get(event_type, []))
                            callbacks.extend(self._subscribers.get("*", []))
                            for callback in callbacks:
                                await callback(event)
                            await self._redis.xack(stream_key, self._consumer_group, msg_id)
                        except Exception:
                            logger.exception("Error processing message %s", msg_id)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error in listen loop for %s, retrying...", event_type)
                await asyncio.sleep(1)

    async def stop_listening(self) -> None:
        """Stop all background listener tasks."""
        self._running = False
        for task in self._listen_tasks.values():
            task.cancel()
        self._listen_tasks.clear()
