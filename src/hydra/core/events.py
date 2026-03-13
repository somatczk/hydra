"""Complete event hierarchy for the Hydra event-driven architecture.

All events are frozen dataclasses inheriting from ``Event``.
They are serializable via msgpack for Redis Streams transport.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from hydra.core.types import (
    OHLCV,
    Direction,
    ExchangeId,
    MarketType,
    OrderRequest,
    OrderStatus,
    OrderType,
    Side,
    Symbol,
    Timeframe,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_event_id() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Base event
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Event:
    """Base event. All events carry a unique id and UTC timestamp."""

    event_id: str = field(default_factory=_new_event_id)
    timestamp: datetime = field(default_factory=_utcnow)
    event_type: str = field(default="event", init=False)


# ---------------------------------------------------------------------------
# Market data events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketDataEvent(Event):
    """Base class for all market data events."""

    event_type: str = field(default="market_data", init=False)


@dataclass(frozen=True, slots=True)
class BarEvent(MarketDataEvent):
    """A completed OHLCV bar."""

    symbol: Symbol = ""
    timeframe: Timeframe = Timeframe.M1
    ohlcv: OHLCV | None = None
    exchange_id: ExchangeId = "binance"
    event_type: str = field(default="bar", init=False)


@dataclass(frozen=True, slots=True)
class TradeEvent(MarketDataEvent):
    """A single trade tick from the exchange."""

    symbol: Symbol = ""
    price: Decimal = Decimal("0")
    quantity: Decimal = Decimal("0")
    side: Side = Side.BUY
    exchange_id: ExchangeId = "binance"
    trade_id: str = ""
    event_type: str = field(default="trade", init=False)


@dataclass(frozen=True, slots=True)
class OrderBookEvent(MarketDataEvent):
    """Order book snapshot or delta."""

    symbol: Symbol = ""
    bids: tuple[tuple[Decimal, Decimal], ...] = ()
    asks: tuple[tuple[Decimal, Decimal], ...] = ()
    exchange_id: ExchangeId = "binance"
    event_type: str = field(default="order_book", init=False)


@dataclass(frozen=True, slots=True)
class FundingRateEvent(MarketDataEvent):
    """Perpetual futures funding rate update."""

    symbol: Symbol = ""
    rate: Decimal = Decimal("0")
    next_funding_time: datetime | None = None
    exchange_id: ExchangeId = "binance"
    event_type: str = field(default="funding_rate", init=False)


# ---------------------------------------------------------------------------
# Signal events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SignalEvent(Event):
    """Base class for strategy-generated signals."""

    event_type: str = field(default="signal", init=False)


@dataclass(frozen=True, slots=True)
class EntrySignal(SignalEvent):
    """Signal to enter a position."""

    symbol: Symbol = ""
    direction: Direction = Direction.LONG
    strength: Decimal = Decimal("0")
    strategy_id: str = ""
    exchange_id: ExchangeId = "binance"
    market_type: MarketType = MarketType.SPOT
    event_type: str = field(default="entry_signal", init=False)


@dataclass(frozen=True, slots=True)
class ExitSignal(SignalEvent):
    """Signal to exit a position."""

    symbol: Symbol = ""
    direction: Direction = Direction.FLAT
    strategy_id: str = ""
    exchange_id: ExchangeId = "binance"
    reason: str = ""
    event_type: str = field(default="exit_signal", init=False)


# ---------------------------------------------------------------------------
# Order events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OrderEvent(Event):
    """Base class for order lifecycle events."""

    event_type: str = field(default="order", init=False)


@dataclass(frozen=True, slots=True)
class OrderRequestEvent(OrderEvent):
    """An order has been requested."""

    order_request: OrderRequest | None = None
    event_type: str = field(default="order_request", init=False)


@dataclass(frozen=True, slots=True)
class OrderFillEvent(OrderEvent):
    """An order has been filled (fully or partially)."""

    order_id: str = ""
    symbol: Symbol = ""
    side: Side = Side.BUY
    order_type: OrderType = OrderType.MARKET
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    fee: Decimal = Decimal("0")
    fee_currency: str = ""
    exchange_id: ExchangeId = "binance"
    status: OrderStatus = OrderStatus.FILLED
    event_type: str = field(default="order_fill", init=False)


@dataclass(frozen=True, slots=True)
class OrderCancelEvent(OrderEvent):
    """An order has been cancelled."""

    order_id: str = ""
    symbol: Symbol = ""
    exchange_id: ExchangeId = "binance"
    reason: str = ""
    event_type: str = field(default="order_cancel", init=False)


@dataclass(frozen=True, slots=True)
class OrderRejectEvent(OrderEvent):
    """An order has been rejected."""

    order_id: str = ""
    symbol: Symbol = ""
    exchange_id: ExchangeId = "binance"
    reason: str = ""
    event_type: str = field(default="order_reject", init=False)


# ---------------------------------------------------------------------------
# Risk events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RiskEvent(Event):
    """Base class for risk-related events."""

    event_type: str = field(default="risk", init=False)


@dataclass(frozen=True, slots=True)
class RiskCheckResult(RiskEvent):
    """Result of a pre-trade risk check."""

    order_request_id: str = ""
    approved: bool = True
    reason: str = ""
    event_type: str = field(default="risk_check_result", init=False)


@dataclass(frozen=True, slots=True)
class CircuitBreakerEvent(RiskEvent):
    """Circuit breaker state change."""

    tier: int = 0
    action: str = ""
    drawdown_pct: Decimal = Decimal("0")
    event_type: str = field(default="circuit_breaker", init=False)


@dataclass(frozen=True, slots=True)
class DrawdownAlertEvent(RiskEvent):
    """Drawdown threshold breach alert."""

    current_drawdown_pct: Decimal = Decimal("0")
    threshold_pct: Decimal = Decimal("0")
    message: str = ""
    event_type: str = field(default="drawdown_alert", init=False)


# ---------------------------------------------------------------------------
# System events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SystemEvent(Event):
    """Base class for system-level events."""

    event_type: str = field(default="system", init=False)


@dataclass(frozen=True, slots=True)
class HeartbeatEvent(SystemEvent):
    """Periodic heartbeat for liveness checks."""

    component: str = ""
    event_type: str = field(default="heartbeat", init=False)


@dataclass(frozen=True, slots=True)
class ConfigChangeEvent(SystemEvent):
    """Configuration has been updated."""

    section: str = ""
    changes: dict[str, Any] = field(default_factory=dict)
    event_type: str = field(default="config_change", init=False)


@dataclass(frozen=True, slots=True)
class ErrorEvent(SystemEvent):
    """An error has occurred in a component."""

    component: str = ""
    error_type: str = ""
    message: str = ""
    traceback: str = ""
    event_type: str = field(default="error", init=False)


# ---------------------------------------------------------------------------
# Serialization helpers (msgpack)
# ---------------------------------------------------------------------------


def event_to_dict(event: Event) -> dict[str, Any]:
    """Serialize an event to a plain dict suitable for msgpack packing."""
    from dataclasses import asdict

    data = asdict(event)
    # Convert non-serializable types
    _convert_values(data)
    data["__event_class__"] = type(event).__name__
    return data


def _convert_values(d: dict[str, Any]) -> None:
    """Recursively convert Decimals, datetimes, and nested dicts for msgpack."""
    for key, value in list(d.items()):
        if isinstance(value, Decimal):
            d[key] = str(value)
        elif isinstance(value, datetime):
            d[key] = value.isoformat()
        elif isinstance(value, dict):
            _convert_values(value)
        elif isinstance(value, (list, tuple)):
            d[key] = _convert_sequence(value)


def _convert_sequence(seq: list[Any] | tuple[Any, ...]) -> list[Any]:
    """Convert sequences for msgpack serialization."""
    result: list[Any] = []
    for item in seq:
        if isinstance(item, Decimal):
            result.append(str(item))
        elif isinstance(item, datetime):
            result.append(item.isoformat())
        elif isinstance(item, dict):
            _convert_values(item)
            result.append(item)
        elif isinstance(item, (list, tuple)):
            result.append(_convert_sequence(item))
        else:
            result.append(item)
    return result


# Registry of event classes for deserialization
_EVENT_CLASSES: dict[str, type[Event]] = {
    "Event": Event,
    "MarketDataEvent": MarketDataEvent,
    "BarEvent": BarEvent,
    "TradeEvent": TradeEvent,
    "OrderBookEvent": OrderBookEvent,
    "FundingRateEvent": FundingRateEvent,
    "SignalEvent": SignalEvent,
    "EntrySignal": EntrySignal,
    "ExitSignal": ExitSignal,
    "OrderEvent": OrderEvent,
    "OrderRequestEvent": OrderRequestEvent,
    "OrderFillEvent": OrderFillEvent,
    "OrderCancelEvent": OrderCancelEvent,
    "OrderRejectEvent": OrderRejectEvent,
    "RiskEvent": RiskEvent,
    "RiskCheckResult": RiskCheckResult,
    "CircuitBreakerEvent": CircuitBreakerEvent,
    "DrawdownAlertEvent": DrawdownAlertEvent,
    "SystemEvent": SystemEvent,
    "HeartbeatEvent": HeartbeatEvent,
    "ConfigChangeEvent": ConfigChangeEvent,
    "ErrorEvent": ErrorEvent,
}


def event_from_dict(data: dict[str, Any]) -> Event:
    """Deserialize a dict (from msgpack) back to an Event instance."""
    data = dict(data)  # shallow copy
    class_name = data.pop("__event_class__", "Event")
    event_cls = _EVENT_CLASSES.get(class_name, Event)

    # Reconstruct datetime fields
    for key in ("timestamp", "next_funding_time"):
        if key in data and isinstance(data[key], str):
            with contextlib.suppress(ValueError):
                data[key] = datetime.fromisoformat(data[key])

    # Reconstruct Decimal fields based on known field types
    _reconstruct_decimals(data, event_cls)

    # Remove event_type as it is set by __init__ (init=False)
    data.pop("event_type", None)

    # Handle nested OHLCV reconstruction
    if "ohlcv" in data and isinstance(data["ohlcv"], dict):
        ohlcv_data = data["ohlcv"]
        for okey in ("open", "high", "low", "close", "volume"):
            if okey in ohlcv_data and isinstance(ohlcv_data[okey], str):
                ohlcv_data[okey] = Decimal(ohlcv_data[okey])
        if "timestamp" in ohlcv_data and isinstance(ohlcv_data["timestamp"], str):
            ohlcv_data["timestamp"] = datetime.fromisoformat(ohlcv_data["timestamp"])
        data["ohlcv"] = OHLCV(**ohlcv_data)

    # Handle nested OrderRequest reconstruction
    if "order_request" in data and isinstance(data["order_request"], dict):
        req_data = data["order_request"]
        req_data.pop("event_type", None)
        for rkey in ("quantity", "price", "stop_price"):
            if rkey in req_data and isinstance(req_data[rkey], str):
                req_data[rkey] = Decimal(req_data[rkey])
        data["order_request"] = OrderRequest(**req_data)

    # Handle bids/asks tuples
    for key in ("bids", "asks"):
        if key in data and isinstance(data[key], list):
            data[key] = tuple(
                tuple(Decimal(v) if isinstance(v, str) else v for v in pair) for pair in data[key]
            )

    try:
        return event_cls(**data)
    except TypeError:
        # If constructor fails with extra keys, filter to known fields
        import dataclasses

        valid_fields = {f.name for f in dataclasses.fields(event_cls)}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return event_cls(**filtered)


def _reconstruct_decimals(data: dict[str, Any], cls: type[Event]) -> None:
    """Convert string values back to Decimal for known Decimal fields."""
    import dataclasses

    decimal_fields = set()
    try:
        for f in dataclasses.fields(cls):
            if f.type in ("Decimal", "decimal.Decimal") or (
                isinstance(f.type, str) and "Decimal" in f.type
            ):
                decimal_fields.add(f.name)
    except TypeError:
        return

    for key in decimal_fields:
        if key in data and isinstance(data[key], str):
            data[key] = Decimal(data[key])
