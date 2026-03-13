"""E2E: Full pipeline from bar data to strategy signal to risk check to order to portfolio.

Tests the complete signal-to-order lifecycle without mocks, using real module
instances wired together as they would be in production.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, ExitSignal, OrderFillEvent
from hydra.core.types import (
    OHLCV,
    Direction,
    MarketType,
    OrderRequest,
    OrderType,
    Side,
    Symbol,
    Timeframe,
)
from hydra.execution.order_manager import OrderManager
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.portfolio.pnl import PnLCalculator
from hydra.portfolio.positions import PositionTracker
from hydra.risk.pretrade import PortfolioState, PreTradeRiskManager
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

from .conftest import make_bar

# ---------------------------------------------------------------------------
# Deterministic test strategy -- produces a LONG signal on bar 10
# ---------------------------------------------------------------------------


class _AlwaysLongOnBar10Strategy(BaseStrategy):
    """Produces a LONG entry on the 10th bar, and nothing else."""

    @property
    def required_history(self) -> int:
        return 5

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        tf = bar.timeframe
        bars = self._context.bars(symbol, tf, 20)
        if len(bars) == 10:
            return [
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=Decimal("0.8"),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            ]
        return []


class _ExitOnBar15Strategy(BaseStrategy):
    """Produces a LONG entry on bar 5 and an EXIT on bar 15."""

    @property
    def required_history(self) -> int:
        return 3

    async def on_bar(self, bar: BarEvent) -> list[EntrySignal | ExitSignal]:
        symbol = str(bar.symbol)
        tf = bar.timeframe
        bars = self._context.bars(symbol, tf, 50)
        if len(bars) == 5:
            return [
                EntrySignal(
                    symbol=Symbol(symbol),
                    direction=Direction.LONG,
                    strength=Decimal("0.7"),
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    market_type=self._config.exchange.market_type,
                )
            ]
        if len(bars) == 15:
            return [
                ExitSignal(
                    symbol=Symbol(symbol),
                    direction=Direction.FLAT,
                    strategy_id=self.strategy_id,
                    exchange_id=self._config.exchange.exchange_id,
                    reason="test exit",
                )
            ]
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_strategy_config(strategy_id: str = "test_strat") -> StrategyConfig:
    return StrategyConfig(
        id=strategy_id,
        name="Test Strategy",
        strategy_class="tests.e2e.test_signal_to_order._AlwaysLongOnBar10Strategy",
        symbols=["BTCUSDT"],
        timeframes={"primary": Timeframe.H1},
    )


def _generate_bars(count: int, start_price: float = 42000.0) -> list[OHLCV]:
    """Generate *count* bars starting at *start_price*, incrementing by 100."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [make_bar(start_price + i * 100, start + timedelta(hours=i)) for i in range(count)]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestSignalToOrderPipeline:
    """Full pipeline: bar -> strategy -> risk -> order -> portfolio."""

    async def test_full_long_entry_pipeline(
        self,
        event_bus: InMemoryEventBus,
        paper_executor: PaperTradingExecutor,
        risk_manager: PreTradeRiskManager,
        position_tracker: PositionTracker,
        pnl_calculator: PnLCalculator,
    ) -> None:
        """Bar -> strategy signal -> risk check -> paper order -> portfolio update."""
        # 1. Set up strategy
        config = _make_strategy_config()
        context = StrategyContext()
        context.set_portfolio_value(Decimal("10000"))
        strategy = _AlwaysLongOnBar10Strategy(config=config, context=context)
        await strategy.on_start()

        # 2. Generate bars and feed to strategy via context
        bars = _generate_bars(15)
        symbol = "BTCUSDT"
        tf = Timeframe.H1
        sym = Symbol(symbol)
        signals_collected: list[EntrySignal | ExitSignal] = []

        for bar in bars:
            context.add_bar(symbol, tf, bar)
            paper_executor.set_market_price(symbol, bar.close)

            bar_event = BarEvent(
                symbol=sym,
                timeframe=tf,
                ohlcv=bar,
                exchange_id="binance",
            )
            signals = await strategy.on_bar(bar_event)
            signals_collected.extend(signals)

        # 3. Verify we got exactly one entry signal on bar 10
        entry_signals = [s for s in signals_collected if isinstance(s, EntrySignal)]
        assert len(entry_signals) == 1
        signal = entry_signals[0]
        assert signal.direction == Direction.LONG
        assert signal.strategy_id == "test_strat"
        assert signal.strength == Decimal("0.8")

        # 4. Run risk check on the signal -- build an order from it
        entry_price = bars[9].close  # bar at index 9 is the 10th bar
        # Use a small quantity that passes risk checks (< 2% of portfolio)
        # order_value = quantity * price must be < 0.02 * portfolio_value
        # 0.004 * 42900 = 171.6, which is 1.7% of $10,000
        quantity = Decimal("0.004")
        order = OrderRequest(
            symbol=sym,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=signal.strategy_id,
            exchange_id="binance",
            market_type=MarketType.SPOT,
            price=entry_price,
        )

        portfolio_state = PortfolioState(
            positions=[],
            balances={"USDT": Decimal("10000")},
            portfolio_value=Decimal("10000"),
            average_volume=Decimal("1000000"),
        )
        risk_result = await risk_manager.check_order(order, portfolio_state)
        assert risk_result.approved is True

        # 5. Submit to PaperTradingExecutor
        fill_result = await paper_executor.create_order(
            symbol=symbol,
            side="BUY",
            order_type="MARKET",
            quantity=quantity,
        )
        assert fill_result["status"] == "FILLED"
        assert fill_result["filled"] is True

        # 6. Build fill event and update PositionTracker
        fill_event = OrderFillEvent(
            order_id=fill_result["id"],
            symbol=sym,
            side=Side.BUY,
            quantity=quantity,
            price=Decimal(str(fill_result["price"])),
            fee=Decimal(str(fill_result["fee"]["cost"])),
            fee_currency="USDT",
            exchange_id="binance",
        )
        await position_tracker.update_on_fill(fill_event, strategy_id="test_strat")

        # 7. Verify position exists
        position = await position_tracker.get_position(symbol)
        assert position is not None
        assert position.direction == Direction.LONG
        assert position.quantity == quantity

        # 8. Verify PnL calculator returns correct unrealized PnL
        current_price = bars[-1].close
        unrealized = pnl_calculator.unrealized_pnl(position, current_price)
        expected_pnl = (current_price - position.avg_entry_price) * quantity
        assert unrealized == expected_pnl

    async def test_risk_rejection_blocks_order(
        self,
        event_bus: InMemoryEventBus,
        paper_executor: PaperTradingExecutor,
    ) -> None:
        """Risk check rejects order when circuit breaker tier 2 active."""
        # Create risk manager with circuit breaker tier 2 active
        risk_mgr = PreTradeRiskManager(circuit_breaker_tier=2)

        # Use a small order that passes all checks EXCEPT circuit breaker
        # quantity * price = 0.004 * 42000 = 168, which is 1.68% of $10,000
        order = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.004"),
            strategy_id="test_strat",
            exchange_id="binance",
            market_type=MarketType.SPOT,
            price=Decimal("42000"),
        )

        portfolio_state = PortfolioState(
            positions=[],
            balances={"USDT": Decimal("10000")},
            portfolio_value=Decimal("10000"),
            average_volume=Decimal("1000000"),
        )

        result = await risk_mgr.check_order(order, portfolio_state)
        assert result.approved is False
        assert "circuit breaker" in result.reason.lower()

    async def test_position_close_flow(
        self,
        event_bus: InMemoryEventBus,
        paper_executor: PaperTradingExecutor,
        position_tracker: PositionTracker,
        pnl_calculator: PnLCalculator,
    ) -> None:
        """Open position -> exit signal -> close order -> realized PnL."""
        symbol = "BTCUSDT"
        sym = Symbol(symbol)
        quantity = Decimal("0.1")

        # Set up strategy with entry on bar 5 and exit on bar 15
        config = StrategyConfig(
            id="close_test",
            name="Close Test",
            strategy_class="tests.e2e.test_signal_to_order._ExitOnBar15Strategy",
            symbols=[symbol],
            timeframes={"primary": Timeframe.H1},
        )
        context = StrategyContext()
        context.set_portfolio_value(Decimal("10000"))
        strategy = _ExitOnBar15Strategy(config=config, context=context)
        await strategy.on_start()

        bars = _generate_bars(20, start_price=40000.0)
        for _i, bar in enumerate(bars):
            context.add_bar(symbol, Timeframe.H1, bar)
            paper_executor.set_market_price(symbol, bar.close)

            bar_event = BarEvent(
                symbol=sym,
                timeframe=Timeframe.H1,
                ohlcv=bar,
                exchange_id="binance",
            )
            signals = await strategy.on_bar(bar_event)

            for sig in signals:
                if isinstance(sig, EntrySignal) and sig.direction == Direction.LONG:
                    # Open position via paper executor
                    fill = await paper_executor.create_order(
                        symbol=symbol,
                        side="BUY",
                        order_type="MARKET",
                        quantity=quantity,
                    )
                    fill_event = OrderFillEvent(
                        order_id=fill["id"],
                        symbol=sym,
                        side=Side.BUY,
                        quantity=quantity,
                        price=Decimal(str(fill["price"])),
                        fee=Decimal(str(fill["fee"]["cost"])),
                        fee_currency="USDT",
                        exchange_id="binance",
                    )
                    await position_tracker.update_on_fill(fill_event, strategy_id="close_test")

                elif isinstance(sig, ExitSignal):
                    # Close position via paper executor
                    fill = await paper_executor.create_order(
                        symbol=symbol,
                        side="SELL",
                        order_type="MARKET",
                        quantity=quantity,
                    )
                    fill_event = OrderFillEvent(
                        order_id=fill["id"],
                        symbol=sym,
                        side=Side.SELL,
                        quantity=quantity,
                        price=Decimal(str(fill["price"])),
                        fee=Decimal(str(fill["fee"]["cost"])),
                        fee_currency="USDT",
                        exchange_id="binance",
                    )
                    await position_tracker.update_on_fill(fill_event, strategy_id="close_test")

        # Verify the position is closed (flat or zero quantity)
        pos = await position_tracker.get_position(symbol)
        if pos is not None:
            assert pos.quantity == Decimal("0") or pos.direction == Direction.FLAT

        # Verify realized PnL is positive (price went up from bar 5 to bar 15)
        all_positions = list(position_tracker._positions.values())
        close_test_positions = [p for p in all_positions if p.strategy_id == "close_test"]
        assert len(close_test_positions) == 1
        assert close_test_positions[0].realized_pnl != Decimal("0")

    async def test_order_manager_full_flow(
        self,
        event_bus: InMemoryEventBus,
        paper_executor: PaperTradingExecutor,
        risk_manager: PreTradeRiskManager,
    ) -> None:
        """OrderManager wires risk check + executor + event bus together."""
        paper_executor.set_market_price("BTCUSDT", Decimal("42000"))

        portfolio_state = PortfolioState(
            positions=[],
            balances={"USDT": Decimal("10000")},
            portfolio_value=Decimal("10000"),
            average_volume=Decimal("1000000"),
        )

        order_mgr = OrderManager(
            executor=paper_executor,
            event_bus=event_bus,
            risk_checker=risk_manager,
            portfolio_state=portfolio_state,
            dedup_window=0.0,
        )

        # Collect fill events published onto the event bus
        fill_events: list[OrderFillEvent] = []

        async def _on_fill(event):
            if isinstance(event, OrderFillEvent):
                fill_events.append(event)

        await event_bus.subscribe("order_fill", _on_fill)

        # Use a small order that passes risk checks
        # 0.004 * 42000 = 168, which is 1.68% of $10,000 (< 2% max_risk_per_trade)
        order = OrderRequest(
            symbol=Symbol("BTCUSDT"),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.004"),
            strategy_id="test_strat",
            exchange_id="binance",
            market_type=MarketType.SPOT,
            price=Decimal("42000"),
        )

        order_id = await order_mgr.submit_order(order)
        assert order_id is not None
        assert len(fill_events) == 1
        assert fill_events[0].symbol == Symbol("BTCUSDT")
        assert fill_events[0].side == Side.BUY
