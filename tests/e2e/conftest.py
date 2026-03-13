"""Shared E2E fixtures for end-to-end integration testing.

All fixtures are self-contained and require no external services (no Redis,
no PostgreSQL, no exchange connections).
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import EntrySignal
from hydra.core.time import BacktestClock
from hydra.core.types import OHLCV, Direction, MarketType, Symbol
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.portfolio.pnl import PnLCalculator
from hydra.portfolio.positions import PositionTracker
from hydra.risk.circuit_breakers import CircuitBreakerManager
from hydra.risk.pretrade import PortfolioState, PreTradeRiskManager, RiskConfig
from hydra.strategy.context import StrategyContext

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_bar(
    price: float,
    timestamp: datetime,
    spread_pct: float = 0.02,
    volume: float = 100.0,
) -> OHLCV:
    """Create an OHLCV bar from a close price with realistic spread.

    The open is set equal to close, high is *close + spread_pct/2*, and low
    is *close - spread_pct/2*.  This produces deterministic bars suitable for
    testing indicator and strategy logic.
    """
    close_d = Decimal(str(price))
    half_spread = Decimal(str(price * spread_pct / 2))
    return OHLCV(
        open=close_d,
        high=close_d + half_spread,
        low=close_d - half_spread,
        close=close_d,
        volume=Decimal(str(volume)),
        timestamp=timestamp,
    )


def make_entry_signal(
    symbol: str = "BTCUSDT",
    direction: Direction = Direction.LONG,
    strategy_id: str = "test_strategy",
    exchange_id: str = "binance",
    strength: str = "0.5",
) -> EntrySignal:
    """Create an EntrySignal for testing."""
    return EntrySignal(
        symbol=Symbol(symbol),
        direction=direction,
        strength=Decimal(strength),
        strategy_id=strategy_id,
        exchange_id=exchange_id,
        market_type=MarketType.SPOT,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def event_bus() -> InMemoryEventBus:
    """Fresh InMemoryEventBus for each test."""
    return InMemoryEventBus()


@pytest.fixture()
def backtest_clock() -> BacktestClock:
    """BacktestClock starting at 2024-01-01 00:00 UTC."""
    return BacktestClock(start=datetime(2024, 1, 1, tzinfo=UTC))


@pytest.fixture()
def strategy_context() -> StrategyContext:
    """Clean StrategyContext for each test."""
    return StrategyContext()


@pytest.fixture()
def paper_executor() -> PaperTradingExecutor:
    """PaperTradingExecutor with $10,000 USDT starting balance."""
    return PaperTradingExecutor(
        exchange_id="binance",
        initial_balances={"USDT": Decimal("10000")},
        slippage_pct=Decimal("0.001"),
        fee_pct=Decimal("0.001"),
    )


@pytest.fixture()
def risk_manager() -> PreTradeRiskManager:
    """PreTradeRiskManager with default configuration."""
    return PreTradeRiskManager(config=RiskConfig())


@pytest.fixture()
def circuit_breakers(event_bus: InMemoryEventBus) -> CircuitBreakerManager:
    """CircuitBreakerManager wired to the test event bus."""
    return CircuitBreakerManager(event_bus=event_bus)


@pytest.fixture()
def position_tracker() -> PositionTracker:
    """Fresh PositionTracker."""
    return PositionTracker()


@pytest.fixture()
def pnl_calculator() -> PnLCalculator:
    """Stateless PnLCalculator."""
    return PnLCalculator()


@pytest.fixture()
def portfolio_state() -> PortfolioState:
    """Default PortfolioState with $10,000 value and USDT balance."""
    return PortfolioState(
        positions=[],
        balances={"USDT": Decimal("10000")},
        daily_pnl=Decimal("0"),
        consecutive_losses=0,
        current_drawdown=Decimal("0"),
        portfolio_value=Decimal("10000"),
        average_volume=Decimal("1000000"),
    )


@pytest.fixture()
def sample_bars() -> list[OHLCV]:
    """Generate 200 bars of realistic BTC price data.

    The series trends up from 40,000 to approximately 48,000 over the first
    100 bars, then trends back down to approximately 40,000 over the next 100
    bars.  A small sinusoidal noise component is added for realism.
    """
    bars: list[OHLCV] = []
    base_price = 40000.0
    start = datetime(2024, 1, 1, tzinfo=UTC)

    for i in range(200):
        trend = base_price + i * 80 if i < 100 else base_price + (200 - i) * 80

        # Add deterministic sinusoidal noise for volatility
        noise = 200 * math.sin(i * 0.3)
        price = trend + noise
        ts = start + timedelta(hours=i)
        bars.append(make_bar(price, ts, spread_pct=0.02, volume=500.0))

    return bars
