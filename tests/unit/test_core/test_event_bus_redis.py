"""The opt-in Redis fan-out on a stubbed client: the mirror's failure semantics.

The publisher's contract is the point: ``on_event`` never awaits the wire and never
raises, a full buffer and a failed XADD drop events that are counted, and the drain
loop survives every failure. The subscriber's contract: redeliveries are deduped,
callbacks are isolated, and the XACK lands after them. The wire format itself (every
event class through the publisher's msgpack path and back) is pinned here too.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import msgpack
import pytest
from prometheus_client import REGISTRY

from hydra.core import metrics
from hydra.core.event_bus import RedisEventPublisher, RedisEventSubscriber
from hydra.core.events import (
    BarEvent,
    CircuitBreakerEvent,
    ConfigChangeEvent,
    DrawdownAlertEvent,
    EntrySignal,
    ErrorEvent,
    Event,
    ExitSignal,
    FeedStatusEvent,
    FundingRateEvent,
    HeartbeatEvent,
    MarketDataEvent,
    OrderBookEvent,
    OrderCancelEvent,
    OrderEvent,
    OrderFillEvent,
    OrderRejectEvent,
    OrderRequestEvent,
    PriceEvent,
    RiskCheckResult,
    RiskEvent,
    SignalEvent,
    SystemEvent,
    TradeEvent,
    event_from_dict,
    event_to_dict,
)
from hydra.core.types import (
    OHLCV,
    Direction,
    MarketType,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    Timeframe,
)

# ---------------------------------------------------------------------------
# The stub redis client
# ---------------------------------------------------------------------------


class StubPipeline:
    def __init__(self, stub: StubRedis) -> None:
        self._stub = stub
        self.batch: list[tuple[str, dict[bytes, bytes]]] = []

    def xadd(
        self,
        stream: str,
        fields: dict[bytes, bytes],
        maxlen: int | None = None,
        approximate: bool = True,
    ) -> None:
        self.batch.append((stream, fields))

    async def execute(self) -> None:
        if self._stub.xadd_delay_s is not None:
            await asyncio.sleep(self._stub.xadd_delay_s)
        if self._stub.fail_execute:
            self._stub.failed_batches += 1
            raise ConnectionError("stub redis outage")
        self._stub.xadd_calls.extend(self.batch)

    async def __aenter__(self) -> StubPipeline:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class StubRedis:
    """The client surface the publisher and subscriber use, with failure knobs."""

    def __init__(
        self,
        *,
        xadd_delay_s: float | None = None,
        fail_execute: bool = False,
        ping_fails: bool = False,
    ) -> None:
        self.xadd_delay_s = xadd_delay_s
        self.fail_execute = fail_execute
        self.ping_fails = ping_fails
        self.xadd_calls: list[tuple[str, dict[bytes, bytes]]] = []
        self.failed_batches = 0
        self.read_batches: list[list[tuple[bytes, dict[bytes, bytes]]]] = []
        self.xack_calls: list[bytes] = []
        self.closed = False

    async def ping(self) -> bool:
        if self.ping_fails:
            raise ConnectionError("stub redis unreachable")
        return True

    def pipeline(self, transaction: bool = False) -> StubPipeline:
        return StubPipeline(self)

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int = 10,
        block: int = 1000,
    ) -> list[list[Any]]:
        await asyncio.sleep(0.01)  # a bounded fake of the blocking read
        if not self.read_batches:
            return []
        return [[b"stream", self.read_batches.pop(0)]]

    async def xautoclaim(
        self,
        stream: str,
        group: str,
        consumer: str,
        min_idle_time: int,
        start_id: str = "0-0",
        count: int | None = None,
    ) -> list[Any]:
        return [b"0-0", [], []]

    async def xgroup_create(
        self, stream: str, group: str, id: str = "0", mkstream: bool = False
    ) -> None:
        return None

    async def xack(self, stream: str, group: str, *msg_ids: bytes) -> None:
        self.xack_calls.extend(msg_ids)

    async def aclose(self) -> None:
        self.closed = True


def _drop(event_type: str, reason: str) -> float | None:
    return REGISTRY.get_sample_value(
        "hydra_event_bus_redis_drops_total", {"event_type": event_type, "reason": reason}
    )


async def _until(condition: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within the deadline")


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Iterator[None]:
    metrics._reset_metrics()
    yield
    metrics._reset_metrics()


def _heartbeats(n: int) -> list[Event]:
    return [HeartbeatEvent(component=f"c{i}") for i in range(n)]


# ---------------------------------------------------------------------------
# The publisher: failure semantics of the mirror
# ---------------------------------------------------------------------------


class TestPublisherNeverFailsTheBarPath:
    async def test_a_raising_xadd_never_raises_and_counts_every_drop(self) -> None:
        stub = StubRedis(fail_execute=True)
        publisher = RedisEventPublisher("redis://stub", client=stub)
        await publisher.connect()

        for event in _heartbeats(100):  # no exception may escape the mirror
            await publisher.on_event(event)
        await _until(lambda: (_drop("heartbeat", "publish_failed") or 0) >= 100)

        # the drain loop survived: a second round is counted too
        for event in _heartbeats(20):
            await publisher.on_event(event)
        await _until(lambda: (_drop("heartbeat", "publish_failed") or 0) >= 120)
        assert publisher._drain_task is not None and not publisher._drain_task.done()
        assert stub.failed_batches >= 1
        await publisher.close()

    async def test_publish_never_awaits_the_wire(self) -> None:
        stub = StubRedis(xadd_delay_s=999.0)  # the wire hangs
        publisher = RedisEventPublisher("redis://stub", client=stub)
        await publisher.connect()
        await asyncio.sleep(0.05)  # the drain picks up the first event and hangs

        started = time.perf_counter()
        for event in _heartbeats(100):
            await publisher.on_event(event)
        elapsed = time.perf_counter() - started

        assert elapsed < 0.01, f"100 mirrors took {elapsed * 1000:.1f} ms"
        await publisher.close()  # cancels the hung drain without hanging itself

    async def test_the_bounded_buffer_drops_the_overflow_and_xadds_the_rest(self) -> None:
        stub = StubRedis()
        publisher = RedisEventPublisher("redis://stub", client=stub, buffer_size=8)

        for event in _heartbeats(20):  # no drain yet: the buffer is all there is
            await publisher.on_event(event)
        assert publisher._queue.qsize() == 8
        assert _drop("heartbeat", "buffer_full") == 12.0

        await publisher.connect()
        await _until(lambda: len(stub.xadd_calls) == 8)
        assert _drop("heartbeat", "publish_failed") is None
        streams = {stream for stream, _fields in stub.xadd_calls}
        assert streams == {"hydra:events:heartbeat"}
        await publisher.close()

    async def test_an_unserializable_event_is_dropped_not_raised(self) -> None:
        publisher = RedisEventPublisher("redis://stub", client=StubRedis())
        # msgpack cannot pack a plain object: a real serialization failure
        event = ConfigChangeEvent(section="s", changes={"bad": object()})

        await publisher.on_event(event)

        assert _drop("config_change", "publish_failed") == 1.0
        await publisher.close()

    async def test_connect_ping_failures_raise_for_the_caller_to_fail_fast(self) -> None:
        stub = StubRedis(ping_fails=True)
        publisher = RedisEventPublisher("redis://stub", client=stub, retry_budget_s=0.05)
        with pytest.raises(ConnectionError):
            await publisher.connect()
        assert publisher._drain_task is None
        await publisher.close()


# ---------------------------------------------------------------------------
# The subscriber: dedup, isolation, ack
# ---------------------------------------------------------------------------


def _entry(event_id: str) -> EntrySignal:
    return EntrySignal(
        event_id=event_id,
        symbol=Symbol("BTCUSDT"),
        direction=Direction.LONG,
        strength=Decimal("0.7"),
        strategy_id="stub",
        exchange_id="binance",
        market_type=MarketType.SPOT,
    )


def _message(event: Event, msg_id: bytes) -> tuple[bytes, dict[bytes, bytes]]:
    return msg_id, {b"data": msgpack.packb(event_to_dict(event), use_bin_type=True)}


class TestSubscriberDelivery:
    async def test_a_redelivery_is_deduped_and_still_acked(self) -> None:
        stub = StubRedis()
        subscriber = RedisEventSubscriber("redis://stub", client=stub, group="g")
        duplicate = _entry("same-id")
        stub.read_batches = [
            [_message(duplicate, b"1-1")],
            [_message(duplicate, b"1-2")],  # a true redelivery: new entry, same event
        ]
        received: list[Event] = []

        async def record(event: Event) -> None:
            received.append(event)

        await subscriber.connect()
        await subscriber.subscribe("entry_signal", record)
        await subscriber.start_listening()
        try:
            await _until(lambda: len(stub.xack_calls) == 2)
        finally:
            await subscriber.close()
        assert len(received) == 1
        assert received[0].event_id == "same-id"

    async def test_a_raising_callback_never_stops_the_others(self) -> None:
        stub = StubRedis()
        subscriber = RedisEventSubscriber("redis://stub", client=stub, group="g")
        stub.read_batches = [[_message(_entry("a"), b"1-1"), _message(_entry("b"), b"1-2")]]
        received: list[Event] = []

        async def broken(event: Event) -> None:
            raise ValueError("consumer bug")

        async def record(event: Event) -> None:
            received.append(event)

        await subscriber.connect()
        await subscriber.subscribe("entry_signal", broken)
        await subscriber.subscribe("entry_signal", record)
        await subscriber.start_listening()
        try:
            await _until(lambda: len(stub.xack_calls) == 2)
        finally:
            await subscriber.close()
        assert [e.event_id for e in received] == ["a", "b"]
        errors = REGISTRY.get_sample_value(
            "hydra_event_handler_errors_total", {"event_type": "entry_signal"}
        )
        assert errors == 2.0

    async def test_an_undecodable_entry_is_acked_never_redelivered(self) -> None:
        stub = StubRedis()
        subscriber = RedisEventSubscriber("redis://stub", client=stub, group="g")
        stub.read_batches = [[(b"1-1", {b"data": b"\xc1not-msgpack"})]]
        received: list[Event] = []

        async def record(event: Event) -> None:
            received.append(event)

        await subscriber.connect()
        await subscriber.subscribe("entry_signal", record)
        await subscriber.start_listening()
        try:
            await _until(lambda: len(stub.xack_calls) == 1)
            await asyncio.sleep(0.05)  # further iterations reclaim nothing
            assert received == []
            assert len(stub.xack_calls) == 1
        finally:
            await subscriber.close()

    async def test_a_wildcard_subscription_is_refused(self) -> None:
        subscriber = RedisEventSubscriber("redis://stub", client=StubRedis(), group="g")

        async def record(event: Event) -> None:
            return None

        with pytest.raises(ValueError, match="concrete event types"):
            await subscriber.subscribe("*", record)


# ---------------------------------------------------------------------------
# The wire format: every registered event class through the mirror's path
# ---------------------------------------------------------------------------


def _sample_events() -> dict[str, Event]:
    ts = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    bar = OHLCV(Decimal("1"), Decimal("2"), Decimal("0.5"), Decimal("1.5"), Decimal("100"), ts)
    order = OrderRequest(
        symbol=Symbol("BTCUSDT"),
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal("0.5"),
        strategy_id="s",
        exchange_id="binance",
        market_type=MarketType.SPOT,
        price=Decimal("60000"),
        stop_price=Decimal("57000"),
        tag="entry",
    )
    return {
        "Event": Event(),
        "MarketDataEvent": MarketDataEvent(),
        "BarEvent": BarEvent(
            symbol=Symbol("BTCUSDT"), timeframe=Timeframe.H1, ohlcv=bar, replay=True
        ),
        "PriceEvent": PriceEvent(symbol=Symbol("BTCUSDT"), price=Decimal("60000")),
        "TradeEvent": TradeEvent(
            symbol=Symbol("BTCUSDT"),
            price=Decimal("60000"),
            quantity=Decimal("0.1"),
            side=Side.BUY,
            trade_id="t1",
        ),
        "OrderBookEvent": OrderBookEvent(
            symbol=Symbol("BTCUSDT"),
            bids=((Decimal("1"), Decimal("2")),),
            asks=((Decimal("1.5"), Decimal("3")),),
        ),
        "FundingRateEvent": FundingRateEvent(
            symbol=Symbol("BTCUSDT"),
            rate=Decimal("0.0001"),
            next_funding_time=ts + timedelta(hours=8),
        ),
        "SignalEvent": SignalEvent(),
        "EntrySignal": EntrySignal(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.LONG,
            strength=Decimal("0.9"),
            strategy_id="s",
            market_type=MarketType.FUTURES,
        ),
        "ExitSignal": ExitSignal(
            symbol=Symbol("BTCUSDT"),
            direction=Direction.FLAT,
            strategy_id="s",
            reason="stop",
            close_pct=0.5,
        ),
        "OrderEvent": OrderEvent(),
        "OrderRequestEvent": OrderRequestEvent(order_request=order),
        "OrderFillEvent": OrderFillEvent(
            order_id="hyd-1",
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            quantity=Decimal("0.5"),
            price=Decimal("60000"),
            fee=Decimal("30"),
            fee_currency="USDT",
            status=OrderStatus.FILLED,
            session_id="sess",
            strategy_id="s",
            tag="entry",
            reduce_only=False,
            realized_pnl=None,
            gross_pnl=None,
            mark_source="live",
        ),
        "OrderCancelEvent": OrderCancelEvent(
            order_id="hyd-2", symbol=Symbol("BTCUSDT"), reason="stale"
        ),
        "OrderRejectEvent": OrderRejectEvent(
            order_id="hyd-3", symbol=Symbol("BTCUSDT"), reason="risk"
        ),
        "RiskEvent": RiskEvent(),
        "RiskCheckResult": RiskCheckResult(order_request_id="req-1", approved=False, reason="size"),
        "CircuitBreakerEvent": CircuitBreakerEvent(
            tier=2, action="pause", drawdown_pct=Decimal("12.5")
        ),
        "DrawdownAlertEvent": DrawdownAlertEvent(
            current_drawdown_pct=Decimal("11"), threshold_pct=Decimal("10"), message="dd"
        ),
        "SystemEvent": SystemEvent(),
        "HeartbeatEvent": HeartbeatEvent(component="feed"),
        "ConfigChangeEvent": ConfigChangeEvent(section="risk", changes={"max_position_pct": 20}),
        "ErrorEvent": ErrorEvent(
            component="feed", error_type="ValueError", message="bad", traceback="tb"
        ),
        "FeedStatusEvent": FeedStatusEvent(
            symbol=Symbol("BTCUSDT"),
            timeframe=Timeframe.M1,
            status="stale",
            reason="ws_silence",
            age_seconds=61.0,
        ),
    }


class TestTheWireFormat:
    def test_every_registered_event_class_survives_the_mirror_roundtrip(self) -> None:
        """The registry's 24 classes, each through the publisher's pack and back.

        A new event class must extend this test: an unknown ``__event_class__``
        silently falls back to a bare ``Event`` on the consumer side.
        """
        from hydra.core import events as events_module

        samples = _sample_events()
        assert set(samples) == set(events_module._EVENT_CLASSES)
        for name, event in samples.items():
            # the publisher's exact serialization, then the subscriber's exact parse
            payload = msgpack.packb(event_to_dict(event), use_bin_type=True)
            restored = event_from_dict(msgpack.unpackb(payload, raw=False))
            assert type(restored) is type(event), name
            assert restored.event_id == event.event_id, name
            assert restored.timestamp == event.timestamp, name
            assert restored.event_type == event.event_type, name

    def test_nested_values_survive_the_roundtrip(self) -> None:
        samples = _sample_events()
        fill = samples["OrderFillEvent"]
        restored = event_from_dict(
            msgpack.unpackb(msgpack.packb(event_to_dict(fill), use_bin_type=True), raw=False)
        )
        assert isinstance(restored, OrderFillEvent)
        assert restored.quantity == Decimal("0.5")
        assert restored.price == Decimal("60000")
        assert restored.mark_source == "live"
        assert restored.status is OrderStatus.FILLED
        bar = samples["BarEvent"]
        assert isinstance(bar.ohlcv, OHLCV)
        restored_bar = event_from_dict(
            msgpack.unpackb(msgpack.packb(event_to_dict(bar), use_bin_type=True), raw=False)
        )
        assert isinstance(restored_bar, BarEvent)
        assert restored_bar.ohlcv == bar.ohlcv
        assert restored_bar.replay is True
