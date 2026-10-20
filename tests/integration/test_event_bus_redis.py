"""Integration: RedisEventBus publish/subscribe roundtrip against HYDRA_REDIS_URL."""

from __future__ import annotations

import pytest

from hydra.core.event_bus import RedisEventBus
from hydra.core.events import EntrySignal, Event
from hydra.core.types import Direction, MarketType, Symbol

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestRedisEventBus:
    """RedisEventBus with real Redis."""

    async def test_publish_subscribe_roundtrip(self, redis_url: str) -> None:
        """Publish event, verify subscriber receives it."""
        bus = RedisEventBus(
            redis_url=redis_url,
            stream_prefix="hydra:test:events:",
            consumer_group="test_group",
            consumer_name="test_consumer",
        )
        await bus.connect()

        received: list[Event] = []

        async def _callback(event: Event) -> None:
            received.append(event)

        await bus.subscribe("entry_signal", _callback)

        signal = EntrySignal(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            strength="0.7",
            strategy_id="redis_test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )

        await bus.publish(signal)

        # The local callback fires synchronously on publish
        assert len(received) == 1
        assert isinstance(received[0], EntrySignal)

        await bus.disconnect()

    async def test_wildcard_subscriber(self, redis_url: str) -> None:
        """Wildcard '*' subscriber receives all event types."""
        bus = RedisEventBus(
            redis_url=redis_url,
            stream_prefix="hydra:test:wildcard:",
            consumer_group="test_group_wc",
            consumer_name="test_consumer_wc",
        )
        await bus.connect()

        received: list[Event] = []

        async def _callback(event: Event) -> None:
            received.append(event)

        await bus.subscribe("*", _callback)

        signal = EntrySignal(
            symbol=Symbol("ETHUSDT"),
            direction=Direction.SHORT,
            strength="0.5",
            strategy_id="wc_test",
            exchange_id="binance",
            market_type=MarketType.SPOT,
        )

        await bus.publish(signal)
        assert len(received) == 1

        await bus.disconnect()
