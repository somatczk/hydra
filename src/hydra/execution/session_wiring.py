"""Build and wire the per-session runtime: bus, engine, executor, OrderManager and feed."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast

from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import BarEvent, EntrySignal, Event, ExitSignal, OrderFillEvent
from hydra.core.types import (
    Direction,
    ExchangeId,
    MarketType,
    OrderRequest,
    OrderType,
    Side,
    Symbol,
    Timeframe,
)
from hydra.data.ingestion import ExchangeFeedManager
from hydra.execution import client_factory
from hydra.execution.order_manager import OrderManager
from hydra.risk.pretrade import PortfolioState, PreTradeRiskManager
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine

if TYPE_CHECKING:
    from hydra.core.config import HydraConfig
    from hydra.execution.session_manager import TradingSession

logger = logging.getLogger(__name__)


def collect_timeframes(cfg: StrategyConfig) -> list[Timeframe]:
    """Return all timeframes a strategy uses (primary + confirmation + entry)."""
    tfs = [cfg.timeframes.primary]
    if cfg.timeframes.confirmation is not None:
        tfs.append(cfg.timeframes.confirmation)
    if cfg.timeframes.entry is not None:
        tfs.append(cfg.timeframes.entry)
    return tfs


async def build_engine(
    cfg: StrategyConfig, hydra_config: HydraConfig
) -> tuple[InMemoryEventBus, StrategyEngine]:
    """Create the session's event bus, context and engine, and load the strategy."""
    event_bus = InMemoryEventBus()
    context = StrategyContext()
    engine = StrategyEngine(config=hydra_config, event_bus=event_bus, context=context)
    await engine.load_strategy_from_config(cfg)
    return event_bus, engine


async def attach_order_path(
    session: TradingSession,
    cfg: StrategyConfig,
    engine: StrategyEngine,
    event_bus: InMemoryEventBus,
    executor: Any,
    risk_manager: PreTradeRiskManager | None,
    build_state: Callable[[TradingSession], Awaitable[PortfolioState]],
) -> None:
    """Attach executor, OrderManager and feed to *session*."""
    session._executor = executor
    session._event_bus = event_bus

    portfolio_state = await build_state(session)

    async def _state_builder() -> PortfolioState:
        return await build_state(session)

    order_manager = OrderManager(
        executor=executor,
        event_bus=event_bus,
        risk_checker=risk_manager,
        portfolio_state=portfolio_state,
        portfolio_state_builder=_state_builder,
    )
    feed = ExchangeFeedManager(event_bus=event_bus)

    session._engine = engine
    session._order_manager = order_manager
    session._feed = feed
    session._all_timeframes = collect_timeframes(cfg)


async def start_session_runtime(
    session: TradingSession,
    get_risk_manager: Callable[[], PreTradeRiskManager | None],
    get_risk_config: Callable[[], dict[str, Any]],
) -> None:
    """Start the engine, subscribe the session handlers and connect the feed."""
    if session._engine is None:
        return
    await session._engine.start()

    # Wire signal->order bridge
    if session._event_bus and session._order_manager and session._executor:
        om = session._order_manager  # capture for closure

        async def on_bar_price(event: Event) -> None:
            if (
                isinstance(event, BarEvent)
                and event.ohlcv
                and hasattr(session._executor, "set_market_price")
            ):
                session._executor.set_market_price(
                    str(event.symbol),
                    event.ohlcv.close,
                )

        async def on_signal(event: Event) -> None:
            order = await signal_to_order(event, session, get_risk_config)
            if order is not None:
                try:
                    await om.submit_order(order)
                except ValueError as exc:
                    logger.warning("Skipped order from signal: %s", exc)
                except Exception:
                    logger.exception("Failed to submit order from signal")

        await session._event_bus.subscribe("bar", on_bar_price)
        await session._event_bus.subscribe("entry_signal", on_signal)
        await session._event_bus.subscribe("exit_signal", on_signal)

        # Forward fill events to risk manager
        async def on_fill(event: Event) -> None:
            if get_risk_manager() and session.status == "running":
                pnl = Decimal(str(getattr(event, "pnl", 0) or 0))
                balance = await session._executor.get_balance()
                portfolio_val = sum(balance.values(), Decimal("0"))
                await cast(PreTradeRiskManager, get_risk_manager()).record_fill(pnl, portfolio_val)

        await session._event_bus.subscribe("order_fill", on_fill)

        # Publish fills to WebSocket ConnectionManager
        async def on_fill_ws(event: Event) -> None:
            try:
                from hydra.core.websocket import manager

                fill_data = {
                    "event": "order_fill",
                    "session_id": session.session_id,
                    "order_id": getattr(event, "order_id", ""),
                    "symbol": str(getattr(event, "symbol", "")),
                    "side": str(getattr(event, "side", "")),
                    "quantity": str(getattr(event, "quantity", "0")),
                    "price": str(getattr(event, "price", "0")),
                    "fee": str(getattr(event, "fee", "0")),
                    "timestamp": datetime.now(UTC).isoformat(),
                }
                await manager.broadcast(f"trades:{session.session_id}", fill_data)
            except Exception:
                logger.debug("Could not broadcast fill to WebSocket")

        await session._event_bus.subscribe("order_fill", on_fill_ws)

    # Wire OrderManagementStrategy if applicable
    if (
        session._engine is not None
        and session._engine.is_order_management
        and session._executor is not None
        and session._event_bus is not None
    ):
        from hydra.strategy.base import OrderManagementStrategy

        for strategy in session._engine.get_all_strategies().values():
            if isinstance(strategy, OrderManagementStrategy):
                oms = strategy
                ex = session._executor

                # Execute on_start actions
                start_actions = await oms.on_start()
                if start_actions:
                    await execute_order_actions(start_actions, ex, session)

                # On bar events: call strategy's on_bar -> execute actions
                async def on_bar_oms(
                    event: Event,
                    _oms: Any = oms,
                    _ex: Any = ex,
                ) -> None:
                    if isinstance(event, BarEvent):
                        actions = await _oms.on_bar(event)
                        if actions:
                            await execute_order_actions(actions, _ex, session)

                await session._event_bus.subscribe("bar", on_bar_oms)

                # On fill events: call strategy's on_fill -> execute follow-ups
                async def on_fill_oms(
                    event: Event,
                    _oms: Any = oms,
                    _ex: Any = ex,
                ) -> None:
                    if isinstance(event, OrderFillEvent):
                        actions = await _oms.on_fill(event)
                        if actions:
                            await execute_order_actions(actions, _ex, session)

                await session._event_bus.subscribe("order_fill", on_fill_oms)

    # Start market data feed
    if session._feed is not None:
        eid = cast(ExchangeId, session.exchange_id)
        session._feed._exchange_factories[eid] = client_factory.feed_exchange_factory(
            session.exchange_id
        )
        await session._feed.connect(
            exchange_id=eid,
            symbols=session.symbols,
            timeframes=session._all_timeframes or [Timeframe(session.timeframe)],
        )


async def execute_order_actions(actions: list[Any], executor: Any, session: TradingSession) -> None:
    """Dispatch OrderAction items (PlaceOrder/CancelOrder/ModifyOrder) to the executor."""
    from hydra.strategy.base import CancelOrder, ModifyOrder, PlaceOrder

    for action in actions:
        try:
            if isinstance(action, PlaceOrder):
                await executor.create_order(
                    symbol=action.symbol,
                    side=action.side,
                    order_type=action.order_type,
                    quantity=action.quantity,
                    price=action.price,
                    stop_price=action.stop_price,
                    params=action.params or {},
                )
            elif isinstance(action, CancelOrder):
                await executor.cancel_order(action.order_id, action.symbol)
            elif isinstance(action, ModifyOrder):
                await executor.cancel_order(action.order_id, action.symbol)
                if action.new_price is not None or action.new_quantity is not None:
                    await executor.create_order(
                        symbol=action.symbol,
                        side="BUY",
                        order_type="LIMIT",
                        quantity=action.new_quantity or Decimal("0"),
                        price=action.new_price,
                    )
        except Exception:
            logger.exception(
                "Failed to execute order action %s in session %s",
                type(action).__name__,
                session.session_id,
            )


async def signal_to_order(
    event: Event, session: TradingSession, get_risk_config: Callable[[], dict[str, Any]]
) -> OrderRequest | None:
    """Convert a signal event into an OrderRequest for the live context."""
    if isinstance(event, EntrySignal):
        balance = await session._executor.get_balance()
        capital = balance.get("USDT", Decimal("0"))
        if capital <= 0:
            return None

        # Load max_risk_per_trade from risk config
        max_risk_per_trade = Decimal(str(get_risk_config().get("max_risk_per_trade", "0.02")))

        price = await session._executor.get_last_price(str(event.symbol))
        if price <= 0:
            return None

        # Compute portfolio value from balance for sizing
        portfolio_value = capital
        quantity = (portfolio_value * max_risk_per_trade) / price
        quantity = quantity.quantize(Decimal("0.00000001"))
        if quantity <= 0:
            return None

        side = Side.BUY if event.direction == Direction.LONG else Side.SELL
        return OrderRequest(
            symbol=Symbol(str(event.symbol)),
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=event.strategy_id,
            exchange_id=event.exchange_id,
            market_type=event.market_type,
        )

    if isinstance(event, ExitSignal):
        positions = await session._executor.get_positions(str(event.symbol))
        if not positions:
            return None
        pos = positions[0]
        side = Side.SELL if pos.direction == Direction.LONG else Side.BUY
        close_pct = getattr(event, "close_pct", 1.0)
        quantity = pos.quantity * Decimal(str(min(max(close_pct, 0.0), 1.0)))
        return OrderRequest(
            symbol=Symbol(str(event.symbol)),
            side=side,
            order_type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=event.strategy_id,
            exchange_id=event.exchange_id,
            market_type=MarketType.SPOT,
        )

    return None
