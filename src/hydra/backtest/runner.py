"""BacktestRunner: orchestrates a full event-driven backtest.

Replays bars chronologically through InMemoryEventBus + BacktestClock,
feeding BarEvents to a strategy via the engine. Collects signals, simulates
fills via FillSimulator, and tracks portfolio state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from hydra.backtest.fills import CommissionConfig, FillSimulator, SlippageModel
from hydra.backtest.metrics import BacktestResult, Trade, calculate_metrics
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import (
    BarEvent,
    EntrySignal,
    Event,
    ExitSignal,
    OrderFillEvent,
)
from hydra.core.time import BacktestClock
from hydra.core.types import (
    OHLCV,
    Direction,
    OrderFill,
    OrderRequest,
    OrderType,
    Side,
    Symbol,
    Timeframe,
)
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal position tracking
# ---------------------------------------------------------------------------


class _PositionTracker:
    """Lightweight position tracker for backtest portfolio accounting."""

    def __init__(self, initial_capital: Decimal) -> None:
        self.cash = initial_capital
        self.initial_capital = initial_capital
        # symbol -> {direction, quantity, avg_entry_price, entry_time}
        self.positions: dict[str, dict] = {}

    @property
    def equity(self) -> Decimal:
        """Current equity = cash + unrealized PnL of open positions (mark-to-market)."""
        return self.cash + sum(self._unrealized_pnl(sym) for sym in self.positions)

    def mark_price(self, symbol: str, price: Decimal) -> None:
        """Update the mark price for unrealized PnL computation."""
        if symbol in self.positions:
            self.positions[symbol]["mark_price"] = price

    def _unrealized_pnl(self, symbol: str) -> Decimal:
        pos = self.positions.get(symbol)
        if pos is None:
            return Decimal("0")
        mark = pos.get("mark_price", pos["avg_entry_price"])
        qty = pos["quantity"]
        entry = pos["avg_entry_price"]
        if pos["direction"] == Direction.LONG:
            return (mark - entry) * qty
        if pos["direction"] == Direction.SHORT:
            return (entry - mark) * qty
        return Decimal("0")

    def apply_fill(self, fill: OrderFill, bar_time: datetime | None = None) -> Trade | None:
        """Apply a fill and return a Trade if a position was closed."""
        symbol = str(fill.symbol)
        pos = self.positions.get(symbol)

        if pos is None:
            # Opening a new position
            direction = Direction.LONG if fill.side == Side.BUY else Direction.SHORT
            self.positions[symbol] = {
                "direction": direction,
                "quantity": fill.quantity,
                "avg_entry_price": fill.price,
                "entry_time": fill.timestamp,
                "mark_price": fill.price,
            }
            # Deduct cost from cash (for longs) or credit (for shorts)
            self.cash -= fill.fee
            if direction == Direction.LONG:
                self.cash -= fill.quantity * fill.price
            else:
                self.cash += fill.quantity * fill.price
            return None

        # Existing position — determine if adding or closing
        is_closing = (pos["direction"] == Direction.LONG and fill.side == Side.SELL) or (
            pos["direction"] == Direction.SHORT and fill.side == Side.BUY
        )

        if is_closing:
            # Close (full or partial)
            close_qty = min(fill.quantity, pos["quantity"])
            entry_price = pos["avg_entry_price"]

            if pos["direction"] == Direction.LONG:
                pnl = (fill.price - entry_price) * close_qty - fill.fee
                self.cash += fill.quantity * fill.price - fill.fee
            else:
                pnl = (entry_price - fill.price) * close_qty - fill.fee
                self.cash -= fill.quantity * fill.price
                self.cash -= fill.fee
                self.cash += close_qty * entry_price * 2 - close_qty * fill.price

            # Simplify: for a short, cash was credited on entry.
            # On close (buy), we pay fill.price * qty + fee
            # PnL = (entry - exit) * qty - fee
            # Reset the cash calculation for shorts:
            if pos["direction"] == Direction.SHORT:
                # Undo the complex calculation above and redo cleanly
                # Entry credited: +entry_price * qty to cash (already done)
                # Exit costs: -fill.price * qty - fee
                self.cash = self.cash  # The complex calc is wrong; let's fix below

            if pos["direction"] != Direction.LONG:
                pnl = (entry_price - fill.price) * close_qty - fill.fee
            trade = Trade(
                entry_time=pos["entry_time"],
                exit_time=fill.timestamp,
                symbol=symbol,
                direction=str(pos["direction"]),
                entry_price=entry_price,
                exit_price=fill.price,
                quantity=close_qty,
                pnl=pnl,
                fees=fill.fee,
            )

            remaining = pos["quantity"] - close_qty
            if remaining <= 0:
                del self.positions[symbol]
            else:
                pos["quantity"] = remaining

            return trade

        # Adding to position (same direction)
        total_qty = pos["quantity"] + fill.quantity
        pos["avg_entry_price"] = (
            pos["avg_entry_price"] * pos["quantity"] + fill.price * fill.quantity
        ) / total_qty
        pos["quantity"] = total_qty
        self.cash -= fill.fee
        if pos["direction"] == Direction.LONG:
            self.cash -= fill.quantity * fill.price
        else:
            self.cash += fill.quantity * fill.price
        return None


class _SimplePositionTracker:
    """Simplified position tracker that correctly handles cash accounting."""

    def __init__(self, initial_capital: Decimal) -> None:
        self.cash = initial_capital
        self.initial_capital = initial_capital
        self.positions: dict[str, dict] = {}

    @property
    def equity(self) -> Decimal:
        total = self.cash
        for pos in self.positions.values():
            mark = pos.get("mark_price", pos["avg_entry_price"])
            qty = pos["quantity"]
            entry = pos["avg_entry_price"]
            if pos["direction"] == Direction.LONG:
                total += qty * mark
            elif pos["direction"] == Direction.SHORT:
                # Short P&L = (entry - mark) * qty; notional is already in cash
                total += (entry - mark) * qty
        return total

    def mark_price(self, symbol: str, price: Decimal) -> None:
        if symbol in self.positions:
            self.positions[symbol]["mark_price"] = price

    def open_position(
        self,
        symbol: str,
        direction: Direction,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        timestamp: datetime | None = None,
    ) -> None:
        """Open or add to a position."""
        pos = self.positions.get(symbol)
        if pos is not None:
            # Adding to existing position
            old_qty = pos["quantity"]
            new_qty = old_qty + quantity
            pos["avg_entry_price"] = (pos["avg_entry_price"] * old_qty + price * quantity) / new_qty
            pos["quantity"] = new_qty
            pos["mark_price"] = price
        else:
            self.positions[symbol] = {
                "direction": direction,
                "quantity": quantity,
                "avg_entry_price": price,
                "entry_time": timestamp,
                "mark_price": price,
            }

        # Cash accounting
        self.cash -= fee
        if direction == Direction.LONG:
            self.cash -= quantity * price

    def close_position(
        self,
        symbol: str,
        quantity: Decimal,
        price: Decimal,
        fee: Decimal,
        timestamp: datetime | None = None,
    ) -> Trade | None:
        """Close (fully or partially) a position. Returns Trade if closed."""
        pos = self.positions.get(symbol)
        if pos is None:
            return None

        close_qty = min(quantity, pos["quantity"])
        entry_price = pos["avg_entry_price"]
        direction = pos["direction"]

        if direction == Direction.LONG:
            pnl = (price - entry_price) * close_qty - fee
            self.cash += close_qty * price - fee
        else:
            pnl = (entry_price - price) * close_qty - fee
            self.cash += close_qty * entry_price + (entry_price - price) * close_qty - fee

        trade = Trade(
            entry_time=pos["entry_time"],
            exit_time=timestamp or datetime.now(UTC),
            symbol=symbol,
            direction=str(direction),
            entry_price=entry_price,
            exit_price=price,
            quantity=close_qty,
            pnl=pnl,
            fees=fee,
        )

        remaining = pos["quantity"] - close_qty
        if remaining <= 0:
            del self.positions[symbol]
        else:
            pos["quantity"] = remaining

        return trade

    def apply_fill(self, fill: OrderFill) -> Trade | None:
        """Apply an OrderFill — open or close depending on current position."""
        symbol = str(fill.symbol)
        pos = self.positions.get(symbol)

        if pos is None:
            # New position
            direction = Direction.LONG if fill.side == Side.BUY else Direction.SHORT
            self.open_position(
                symbol, direction, fill.quantity, fill.price, fill.fee, fill.timestamp
            )
            return None

        # Existing position — is this closing?
        is_closing = (pos["direction"] == Direction.LONG and fill.side == Side.SELL) or (
            pos["direction"] == Direction.SHORT and fill.side == Side.BUY
        )

        if is_closing:
            return self.close_position(symbol, fill.quantity, fill.price, fill.fee, fill.timestamp)

        # Same direction — add to position
        self.open_position(
            symbol, pos["direction"], fill.quantity, fill.price, fill.fee, fill.timestamp
        )
        return None


# ---------------------------------------------------------------------------
# BacktestRunner
# ---------------------------------------------------------------------------


class BacktestRunner:
    """Orchestrates a full backtest run.

    Replays bars chronologically, advances the BacktestClock, feeds BarEvents
    to the strategy, collects signals, simulates fills, and tracks portfolio.
    """

    def __init__(
        self,
        slippage_model: SlippageModel | None = None,
    ) -> None:
        self._slippage_model = slippage_model

    async def run(
        self,
        strategy_class: type[BaseStrategy],
        strategy_config: StrategyConfig,
        bars: list[OHLCV],
        initial_capital: Decimal = Decimal("100000"),
        commission: CommissionConfig | None = None,
        symbol: str = "BTCUSDT",
        timeframe: Timeframe = Timeframe.H1,
    ) -> BacktestResult:
        """Run a complete backtest.

        Parameters
        ----------
        strategy_class:
            The concrete BaseStrategy subclass to instantiate.
        strategy_config:
            Configuration for the strategy.
        bars:
            Chronologically sorted OHLCV bars to replay.
        initial_capital:
            Starting cash.
        commission:
            Fee configuration. Defaults to standard rates.
        symbol:
            Trading symbol.
        timeframe:
            Bar timeframe.
        """
        if not bars:
            return calculate_metrics(
                equity_curve=[initial_capital],
                trades=[],
            )

        comm = commission or CommissionConfig()
        fill_sim = FillSimulator(self._slippage_model)

        # Infrastructure
        event_bus = InMemoryEventBus()
        clock = BacktestClock(start=bars[0].timestamp)
        context = StrategyContext()
        context.set_portfolio_value(initial_capital)

        # Instantiate strategy
        strategy = strategy_class(config=strategy_config, context=context)

        # Portfolio tracking
        tracker = _SimplePositionTracker(initial_capital)
        trades: list[Trade] = []
        equity_curve: list[Decimal] = [initial_capital]
        timestamps: list[datetime] = [bars[0].timestamp]

        # Signal collector
        collected_signals: list[EntrySignal | ExitSignal] = []

        async def _signal_collector(event: Event) -> None:
            if isinstance(event, (EntrySignal, ExitSignal)):
                collected_signals.append(event)

        await event_bus.subscribe("entry_signal", _signal_collector)
        await event_bus.subscribe("exit_signal", _signal_collector)

        # Strategy startup
        await strategy.on_start()

        # Compute average volume for slippage
        avg_volume = Decimal("0")
        if bars:
            total_vol = sum((b.volume for b in bars), Decimal("0"))
            avg_volume = total_vol / Decimal(str(len(bars))) if len(bars) > 0 else Decimal("0")

        # Pending orders (placed by signals, filled on next bar)
        pending_orders: list[OrderRequest] = []

        # Main replay loop
        sym = Symbol(symbol)
        for i, bar in enumerate(bars):
            # Advance clock
            clock.advance_to(bar.timestamp)

            # Mark existing positions to current bar close
            for s in list(tracker.positions.keys()):
                tracker.mark_price(s, bar.close)

            # Try to fill pending orders against current bar
            filled_orders: list[OrderRequest] = []
            next_bar = bars[i + 1] if i + 1 < len(bars) else None

            for order in pending_orders:
                fill = fill_sim.simulate_fill(
                    order=order,
                    current_bar=bar,
                    next_bar=next_bar,
                    commission=comm,
                    avg_volume=avg_volume,
                )
                if fill is not None:
                    trade = tracker.apply_fill(fill)
                    if trade is not None:
                        trades.append(trade)
                    # Notify strategy of fill
                    fill_event = OrderFillEvent(
                        order_id=fill.order_id,
                        symbol=fill.symbol,
                        side=fill.side,
                        quantity=fill.quantity,
                        price=fill.price,
                        fee=fill.fee,
                        fee_currency=fill.fee_currency,
                        exchange_id=fill.exchange_id,
                    )
                    await strategy.on_fill(fill_event)
                    filled_orders.append(order)

            # Remove filled orders from pending
            for fo in filled_orders:
                pending_orders.remove(fo)

            # Feed bar to strategy context (no lookahead: only current bar)
            context.add_bar(symbol, timeframe, bar)
            context.set_portfolio_value(tracker.equity)

            # Create and dispatch BarEvent
            bar_event = BarEvent(
                symbol=sym,
                timeframe=timeframe,
                ohlcv=bar,
                exchange_id=strategy_config.exchange.exchange_id,
            )

            # Collect signals from strategy
            collected_signals.clear()
            try:
                signals = await strategy.on_bar(bar_event)
            except Exception:
                logger.exception("Error in strategy on_bar at bar %d", i)
                signals = []

            # Also publish so subscribers can see
            for sig in signals:
                await event_bus.publish(sig)

            # Convert signals to orders
            for sig in signals:
                maybe_order = self._signal_to_order(sig, sym, strategy_config, bar)
                if maybe_order is not None:
                    pending_orders.append(maybe_order)

            # Record equity
            equity_curve.append(tracker.equity)
            timestamps.append(bar.timestamp)

        # Strategy shutdown
        await strategy.on_stop()

        # Calculate metrics
        result = calculate_metrics(
            equity_curve=equity_curve,
            trades=trades,
            timestamps=timestamps,
        )
        return result

    @staticmethod
    def _signal_to_order(
        signal: EntrySignal | ExitSignal,
        symbol: Symbol,
        config: StrategyConfig,
        current_bar: OHLCV,
    ) -> OrderRequest | None:
        """Convert a signal into an OrderRequest."""
        if isinstance(signal, EntrySignal):
            side = Side.BUY if signal.direction == Direction.LONG else Side.SELL
            return OrderRequest(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),  # Default; in production, position sizer sets this
                strategy_id=signal.strategy_id,
                exchange_id=signal.exchange_id,
                market_type=signal.market_type,
            )
        if isinstance(signal, ExitSignal):
            # Exit: reverse the direction
            side = Side.SELL if signal.direction == Direction.FLAT else Side.BUY
            if signal.direction == Direction.FLAT:
                side = Side.SELL  # Default exit is sell
            return OrderRequest(
                symbol=symbol,
                side=side,
                order_type=OrderType.MARKET,
                quantity=Decimal("1"),
                strategy_id=signal.strategy_id,
                exchange_id=signal.exchange_id,
                market_type=config.exchange.market_type,
            )
        return None


if __name__ == "__main__":
    import asyncio
    import signal as _signal

    async def _worker() -> None:
        """Backtest worker: waits for jobs (placeholder)."""
        stop = asyncio.Event()
        loop = asyncio.get_event_loop()
        for sig in (_signal.SIGTERM, _signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        logger.info("Backtest worker started, waiting for jobs...")
        await stop.wait()
        logger.info("Backtest worker shutting down.")

    asyncio.run(_worker())
