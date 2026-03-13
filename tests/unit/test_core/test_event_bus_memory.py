"""Tests for InMemoryEventBus — publish/subscribe, filtering, ordering, async."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import (
    BarEvent,
    EntrySignal,
    Event,
    HeartbeatEvent,
)
from hydra.core.types import (
    OHLCV,
    Direction,
    Symbol,
    Timeframe,
)


@pytest.fixture
def bus() -> InMemoryEventBus:
    return InMemoryEventBus()


# ---------------------------------------------------------------------------
# Basic publish / subscribe
# ---------------------------------------------------------------------------


class TestPublishSubscribe:
    async def test_subscribe_receives_events(self, bus: InMemoryEventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe("heartbeat", handler)
        hb = HeartbeatEvent(component="test")
        await bus.publish(hb)
        assert len(received) == 1
        assert received[0].event_id == hb.event_id

    async def test_no_duplicate_subscribe(self, bus: InMemoryEventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe("heartbeat", handler)
        await bus.subscribe("heartbeat", handler)  # duplicate — should be ignored
        await bus.publish(HeartbeatEvent(component="test"))
        assert len(received) == 1

    async def test_unsubscribe(self, bus: InMemoryEventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe("heartbeat", handler)
        await bus.publish(HeartbeatEvent(component="a"))
        assert len(received) == 1

        await bus.unsubscribe("heartbeat", handler)
        await bus.publish(HeartbeatEvent(component="b"))
        assert len(received) == 1  # no new events

    async def test_unsubscribe_nonexistent_is_safe(self, bus: InMemoryEventBus) -> None:
        async def handler(event: Event) -> None:
            pass

        # Should not raise
        await bus.unsubscribe("heartbeat", handler)


# ---------------------------------------------------------------------------
# Event type filtering
# ---------------------------------------------------------------------------


class TestEventTypeFiltering:
    async def test_different_types(self, bus: InMemoryEventBus) -> None:
        heartbeats: list[Event] = []
        signals: list[Event] = []

        async def hb_handler(event: Event) -> None:
            heartbeats.append(event)

        async def sig_handler(event: Event) -> None:
            signals.append(event)

        await bus.subscribe("heartbeat", hb_handler)
        await bus.subscribe("entry_signal", sig_handler)

        await bus.publish(HeartbeatEvent(component="a"))
        await bus.publish(
            EntrySignal(
                symbol=Symbol("BTCUSDT"),
                direction=Direction.LONG,
                strength=Decimal("0.9"),
                strategy_id="test",
            )
        )
        await bus.publish(HeartbeatEvent(component="b"))

        assert len(heartbeats) == 2
        assert len(signals) == 1

    async def test_wildcard_subscriber(self, bus: InMemoryEventBus) -> None:
        all_events: list[Event] = []

        async def catch_all(event: Event) -> None:
            all_events.append(event)

        await bus.subscribe("*", catch_all)

        await bus.publish(HeartbeatEvent(component="a"))
        await bus.publish(
            EntrySignal(
                symbol=Symbol("BTCUSDT"),
                direction=Direction.LONG,
                strength=Decimal("0.9"),
                strategy_id="test",
            )
        )

        assert len(all_events) == 2

    async def test_wildcard_and_specific(self, bus: InMemoryEventBus) -> None:
        all_events: list[Event] = []
        hb_events: list[Event] = []

        async def catch_all(event: Event) -> None:
            all_events.append(event)

        async def hb_handler(event: Event) -> None:
            hb_events.append(event)

        await bus.subscribe("*", catch_all)
        await bus.subscribe("heartbeat", hb_handler)

        await bus.publish(HeartbeatEvent(component="a"))
        # heartbeat handler + wildcard both fire
        assert len(all_events) == 1
        assert len(hb_events) == 1


# ---------------------------------------------------------------------------
# Ordering (queued mode)
# ---------------------------------------------------------------------------


class TestOrdering:
    async def test_queued_events_ordered_by_timestamp(self, bus: InMemoryEventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe("bar", handler)

        ts1 = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
        ts3 = datetime(2024, 1, 1, 2, 0, 0, tzinfo=UTC)
        ts2 = datetime(2024, 1, 1, 1, 0, 0, tzinfo=UTC)

        ohlcv = OHLCV(
            Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("100"), ts1
        )

        # Enqueue out of order
        bar3 = BarEvent(
            symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=ohlcv, timestamp=ts3
        )
        bar1 = BarEvent(
            symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=ohlcv, timestamp=ts1
        )
        bar2 = BarEvent(
            symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=ohlcv, timestamp=ts2
        )

        await bus.publish_queued(bar3)
        await bus.publish_queued(bar1)
        await bus.publish_queued(bar2)

        await bus.drain()

        assert len(received) == 3
        assert received[0].timestamp == ts1
        assert received[1].timestamp == ts2
        assert received[2].timestamp == ts3

    async def test_drain_empty_queue(self, bus: InMemoryEventBus) -> None:
        # Should not raise
        await bus.drain()


# ---------------------------------------------------------------------------
# Async behaviour
# ---------------------------------------------------------------------------


class TestAsyncBehaviour:
    async def test_multiple_async_subscribers(self, bus: InMemoryEventBus) -> None:
        results: list[str] = []

        async def handler_a(event: Event) -> None:
            results.append("a")

        async def handler_b(event: Event) -> None:
            results.append("b")

        await bus.subscribe("heartbeat", handler_a)
        await bus.subscribe("heartbeat", handler_b)

        await bus.publish(HeartbeatEvent(component="test"))
        assert "a" in results
        assert "b" in results
        assert len(results) == 2

    async def test_subscriber_count(self, bus: InMemoryEventBus) -> None:
        async def handler(event: Event) -> None:
            pass

        assert bus.subscriber_count("heartbeat") == 0
        await bus.subscribe("heartbeat", handler)
        assert bus.subscriber_count("heartbeat") == 1

    async def test_clear(self, bus: InMemoryEventBus) -> None:
        async def handler(event: Event) -> None:
            pass

        await bus.subscribe("heartbeat", handler)
        await bus.publish_queued(HeartbeatEvent(component="a"))
        bus.clear()
        assert bus.subscriber_count("heartbeat") == 0

    async def test_publish_with_no_subscribers(self, bus: InMemoryEventBus) -> None:
        # Should not raise
        await bus.publish(HeartbeatEvent(component="lonely"))

    async def test_concurrent_publish(self, bus: InMemoryEventBus) -> None:
        received: list[Event] = []

        async def handler(event: Event) -> None:
            received.append(event)

        await bus.subscribe("heartbeat", handler)

        events = [HeartbeatEvent(component=f"c{i}") for i in range(10)]
        await asyncio.gather(*(bus.publish(e) for e in events))

        assert len(received) == 10
