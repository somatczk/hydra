"""Core value objects and type definitions for the Hydra trading platform.

All financial values use Decimal for precision. Value objects are frozen dataclasses
for immutability.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal, NewType, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Simple wrapper types
# ---------------------------------------------------------------------------

Symbol = NewType("Symbol", str)
"""Trading pair symbol, e.g. 'BTCUSDT'."""

ExchangeId = Literal[
    "binance",
    "bybit",
    "kraken",
    "okx",
    "coinbase",
    "kucoin",
    "gateio",
    "mexc",
    "bitget",
]
"""Supported exchange identifiers."""


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Timeframe(StrEnum):
    """OHLCV bar timeframe."""

    M1 = "1m"
    M5 = "5m"
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"


class Side(StrEnum):
    """Order side."""

    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    """Supported order types."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_MARKET = "STOP_MARKET"
    STOP_LIMIT = "STOP_LIMIT"
    TAKE_PROFIT_MARKET = "TAKE_PROFIT_MARKET"
    OCO = "OCO"
    TRAILING_STOP = "TRAILING_STOP"


class OrderStatus(StrEnum):
    """Lifecycle status of an order."""

    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class Direction(StrEnum):
    """Position direction."""

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class MarketType(StrEnum):
    """Market type."""

    SPOT = "SPOT"
    FUTURES = "FUTURES"


# ---------------------------------------------------------------------------
# Frozen dataclasses (immutable value objects)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Executor state protocol (implemented by PaperTradingExecutor & ExchangeClient)
# ---------------------------------------------------------------------------


@runtime_checkable
class ExecutorState(Protocol):
    """Unified async interface for querying executor state."""

    async def get_balance(self) -> dict[str, Decimal]: ...

    async def get_positions(self, symbol: str | None = None) -> list[Position]: ...

    async def get_last_price(self, symbol: str) -> Decimal: ...


# ---------------------------------------------------------------------------
# Frozen dataclasses (immutable value objects)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OHLCV:
    """Single OHLCV bar with Decimal precision."""

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    timestamp: datetime

    def __post_init__(self) -> None:
        if self.timestamp.tzinfo is None:
            object.__setattr__(self, "timestamp", self.timestamp.replace(tzinfo=UTC))


# ---------------------------------------------------------------------------
# Mutable dataclasses (operational objects)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class OrderRequest:
    """Request to place an order."""

    symbol: Symbol
    side: Side
    order_type: OrderType
    quantity: Decimal
    strategy_id: str
    exchange_id: ExchangeId
    market_type: MarketType
    price: Decimal | None = None
    stop_price: Decimal | None = None
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))


@dataclass(slots=True)
class OrderFill:
    """Confirmed order fill from an exchange."""

    order_id: str
    symbol: Symbol
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    timestamp: datetime
    exchange_id: ExchangeId


@dataclass(slots=True)
class Position:
    """Current position state."""

    symbol: Symbol
    direction: Direction
    quantity: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    strategy_id: str
    exchange_id: ExchangeId
