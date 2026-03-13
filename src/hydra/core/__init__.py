"""M01: Core infrastructure -- event bus, types, config, logging."""

from __future__ import annotations

from hydra.core.config import HydraConfig, load_config
from hydra.core.event_bus import InMemoryEventBus, RedisEventBus
from hydra.core.events import (
    BarEvent,
    CircuitBreakerEvent,
    ConfigChangeEvent,
    DrawdownAlertEvent,
    EntrySignal,
    ErrorEvent,
    Event,
    ExitSignal,
    FundingRateEvent,
    HeartbeatEvent,
    OrderBookEvent,
    OrderCancelEvent,
    OrderFillEvent,
    OrderRejectEvent,
    OrderRequestEvent,
    RiskCheckResult,
    TradeEvent,
)
from hydra.core.logging import get_logger, setup_logging
from hydra.core.time import BacktestClock, UTCClock
from hydra.core.types import (
    OHLCV,
    Direction,
    ExchangeId,
    MarketType,
    OrderFill,
    OrderRequest,
    OrderStatus,
    OrderType,
    Position,
    Side,
    Symbol,
    Timeframe,
)

__all__ = [
    "OHLCV",
    "BacktestClock",
    "BarEvent",
    "CircuitBreakerEvent",
    "ConfigChangeEvent",
    "Direction",
    "DrawdownAlertEvent",
    "EntrySignal",
    "ErrorEvent",
    # Events
    "Event",
    "ExchangeId",
    "ExitSignal",
    "FundingRateEvent",
    "HeartbeatEvent",
    # Config
    "HydraConfig",
    # Event bus
    "InMemoryEventBus",
    "MarketType",
    "OrderBookEvent",
    "OrderCancelEvent",
    "OrderFill",
    "OrderFillEvent",
    "OrderRejectEvent",
    "OrderRequest",
    "OrderRequestEvent",
    "OrderStatus",
    "OrderType",
    "Position",
    "RedisEventBus",
    "RiskCheckResult",
    "Side",
    # Types
    "Symbol",
    "Timeframe",
    "TradeEvent",
    # Clock
    "UTCClock",
    "get_logger",
    "load_config",
    # Logging
    "setup_logging",
]
