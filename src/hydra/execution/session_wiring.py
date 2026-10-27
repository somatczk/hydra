"""SessionRuntime: one paper session's bus, context, engine, executor, OrderManager and feed.

Wiring order (fixed): the session's closed-bar handler is subscribed before the engine, so the
paper executor evaluates resting orders and marks to a bar before any strategy sees that bar;
every fill is published before the strategy runs. Then signals, fills, feed status and live
prices (the executor's market-fill mark only; risk marks and resting orders stay on closed bars).
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from hydra.core import metrics
from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.core.events import (
    BarEvent,
    EntrySignal,
    Event,
    ExitSignal,
    FeedStatusEvent,
    OrderCancelEvent,
    OrderFillEvent,
    PriceEvent,
)
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
from hydra.data.bars import timeframe_seconds
from hydra.data.ingestion import ExchangeFeedManager
from hydra.execution import session_store
from hydra.execution.client_factory import ClientFactory
from hydra.execution.errors import SessionNotRunningError, SpotShortError, UnsupportedStrategyError
from hydra.execution.flatten import FlattenResult, flatten_session
from hydra.execution.order_manager import OrderManager, RiskRejectedError
from hydra.execution.paper_trading import (
    LedgerInvariantError,
    PaperFill,
    PaperSessionHaltedError,
    PaperTradingExecutor,
    PersistenceUnavailableError,
)
from hydra.execution.protective_orders import ProtectionSpec, ProtectiveOrderManager
from hydra.risk.service import RiskService
from hydra.strategy.base import BaseStrategy
from hydra.strategy.config import StrategyConfig, config_hash
from hydra.strategy.context import StrategyContext
from hydra.strategy.engine import StrategyEngine, _import_strategy_class

if TYPE_CHECKING:
    from hydra.execution.session_manager import TradingSession

logger = logging.getLogger(__name__)

ENTRY_TAG = "entry"
EXIT_TAG = "exit"
UNPROTECTED_TAG = "unprotected"
MANUAL_TAG_PREFIX = "manual:"
_HALT_ERRORS = (PaperSessionHaltedError, LedgerInvariantError)
_HUNDRED = Decimal("100")

FailureCallback = Callable[["TradingSession", str], Coroutine[Any, Any, None]]
WarmBars = dict[tuple[str, Timeframe], list[OHLCV]]


async def probe_strategy(cfg: StrategyConfig, hydra_config: HydraConfig) -> int:
    """Instantiate the strategy against a throwaway context and return its required history."""
    engine = StrategyEngine(
        config=hydra_config, event_bus=InMemoryEventBus(), context=StrategyContext()
    )
    try:
        cls = _import_strategy_class(cfg.strategy_class)
        if inspect.isabstract(cls):
            raise TypeError("abstract class")
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise UnsupportedStrategyError(
            f"cannot load strategy class {cfg.strategy_class}: {exc}"
        ) from exc
    try:
        await engine.load_strategy_from_config(cfg)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"invalid strategy config for {cfg.id}: {exc}") from exc
    history = [int(s.required_history) for s in engine.get_all_strategies().values()]
    return max(history, default=1)


@dataclass(frozen=True, slots=True)
class ManualOrderResult:
    order_id: str | None
    status: str
    quantity: Decimal
    price: Decimal | None
    protection: str  # 'armed' | 'pending' | 'none'
    protective_order_id: str | None
    fill_price: Decimal | None = None
    mark_source: str | None = None


class SessionRuntime:
    """Owns one running paper session's components; built by ``SessionManager``."""

    def __init__(
        self,
        session: TradingSession,
        cfg: StrategyConfig,
        *,
        executor: PaperTradingExecutor,
        risk: RiskService,
        clients: ClientFactory,
        hydra_config: HydraConfig,
        db_pool: Any,
        required_history: int,
        on_failure: FailureCallback,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.session = session
        self.cfg = cfg
        self._db_pool = db_pool
        self._pending_cfg: StrategyConfig | None = None
        self.executor = executor
        self.risk = risk
        self.exchange_id: ExchangeId = cfg.exchange.exchange_id
        self.market_type: MarketType = cfg.exchange.market_type
        self.primary: Timeframe = cfg.timeframes.primary
        self.required_history = required_history
        self.bus = InMemoryEventBus()
        self.feed = ExchangeFeedManager(
            event_bus=self.bus,
            exchange_factories={self.exchange_id: clients.ws_factory(self.exchange_id)},
            repository=clients.repository_factory(db_pool),
            rest_pool=clients.rest_pool,
            history_window=clients.history_window,
            publish_replay=True,
        )
        self.context = StrategyContext(max_bars=self.feed.window_for(required_history))
        self.engine = StrategyEngine(config=hydra_config, event_bus=self.bus, context=self.context)
        self.om = OrderManager(
            executor,
            self.bus,
            stale_order_timeout=max(300.0, 2.0 * timeframe_seconds(self.primary)),
            risk=risk,
            session_id=session.session_id,
            clock=clock,
        )
        self.protect = ProtectiveOrderManager(
            self.om,
            executor,
            strategy_id=cfg.id,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
            policy=cfg.risk,
            atr=self._atr,
        )
        self._on_failure = on_failure
        self._done = asyncio.Event()
        self.closing = False
        self.entries_paused: str | None = None
        self.spot_shorts_ignored = 0
        self._manual_orders: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Build, warm up, wire, connect
    # ------------------------------------------------------------------

    async def build(self, strategy_class: type[BaseStrategy] | None = None) -> None:
        """Load the strategy into the session engine and expose the parts on the session."""
        await self.engine.load_strategy_from_config(self.cfg, strategy_class)
        s = self.session
        s._runtime = self
        s._engine = self.engine
        s._event_bus = self.bus
        s._order_manager = self.om
        s._executor = self.executor
        s._feed = self.feed
        s._all_timeframes = self.cfg.all_timeframes()

    async def warm_up(self) -> WarmBars:
        """Stored + REST history into the context (raises InsufficientHistoryError)."""
        warm = await self.feed.warm_up(
            self.cfg.symbols,
            self.cfg.all_timeframes(),
            min_bars=self.required_history,
            exchange_id=self.exchange_id,
        )
        await self.engine.warm_up(warm)
        return warm

    async def seed_context(self) -> None:
        """Positions and equity into the strategy context (a restored session may hold some)."""
        for symbol in self.cfg.symbols:
            positions = [p for p in await self.executor.get_positions(symbol) if p.quantity > 0]
            self.context.set_position(symbol, positions[0] if positions else None)
        self.context.set_portfolio_value(await self.executor.get_equity())

    async def wire(self) -> None:
        """Subscribe in order: closed bars, engine, signals, fills, feed status, live prices."""
        await self.bus.subscribe("bar", self._on_bar)
        await self.engine.start()
        await self.bus.subscribe("entry_signal", self._on_signal)
        await self.bus.subscribe("exit_signal", self._on_signal)
        await self.bus.subscribe("order_fill", self._on_fill)
        await self.bus.subscribe("order_cancel", self._on_cancel)
        await self.bus.subscribe("feed_status", self._on_feed_status)
        await self.bus.subscribe("price", self._on_price)

    async def prime(self, warm: WarmBars) -> None:
        """Treat each symbol's newest warm-up bar as the first closed bar: marks, resting orders."""
        for symbol in self.cfg.symbols:
            bars = warm.get((symbol, self.primary)) or []
            if bars:
                await self._process_closed_bar(symbol, bars[-1])

    async def connect(self) -> None:
        """Start the live feed (ValueError: a symbol is not exactly one active spot market)."""
        await self.feed.connect(self.exchange_id, self.cfg.symbols, self.cfg.all_timeframes())

    async def run(self) -> None:
        """The session task: alive while the session runs."""
        await self._done.wait()

    async def shutdown(self) -> None:
        """Stop the feed and the engine and end the task; positions are left to the caller."""
        self._done.set()
        try:
            await self.feed.disconnect_all()
        except Exception:
            logger.exception("Feed disconnect failed for %s", self.session.session_id)
        try:
            await self.engine.stop()
        except Exception:
            logger.exception("Engine stop failed for %s", self.session.session_id)

    async def flatten(self) -> FlattenResult:
        return await flatten_session(self)

    def request_reload(self, cfg: StrategyConfig) -> None:
        """Queue a parameters-only config; ``_on_bar`` applies it before the next closed bar."""
        self._pending_cfg = cfg

    async def _apply_pending_reload(self) -> None:
        cfg, self._pending_cfg = self._pending_cfg, None
        if cfg is None:
            return
        try:
            await self.engine.replace_strategy(cfg)
        except Exception:
            logger.exception(
                "Reload failed for %s; keeping the running config", self.session.session_id
            )
            return
        self.cfg = cfg
        self.session.strategy_config = cfg.model_dump(mode="json")
        self.session.config_hash = config_hash(cfg)
        if not await session_store.persist_session(self._db_pool, self.session, attempts=3):
            logger.error("Reloaded config of %s is not persisted", self.session.session_id)

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _on_bar(self, event: Event) -> None:
        if (
            not isinstance(event, BarEvent)
            or event.ohlcv is None
            or event.replay
            or event.exchange_id != self.exchange_id
            or event.timeframe != self.primary
            or str(event.symbol) not in self.cfg.symbols
        ):
            return
        await self._apply_pending_reload()
        await self._process_closed_bar(str(event.symbol), event.ohlcv)

    async def _process_closed_bar(self, symbol: str, bar: OHLCV) -> None:
        try:
            fills = await self.executor.on_closed_bar(symbol, bar)
        except Exception as exc:
            logger.exception("Paper on_closed_bar failed for %s", self.session.session_id)
            fills = []
            if self.executor.get_account_snapshot().halted is None:
                await self._fail(f"paper executor failed on a closed bar: {exc}")
                return
        for fill in fills:
            try:
                await self.om.on_external_fill(fill)
            except Exception:
                logger.exception("Publishing paper fill %s failed", fill.order_id)
            if fill.status == "CANCELLED":
                await self._release_manual(fill.parent_order_id or fill.order_id, "gap_cancel")
        self.session.last_bar_at = datetime.now(UTC)
        try:
            await self.risk.on_mark(self.exchange_id, symbol, bar.close, bar.timestamp)
        except Exception:
            logger.exception("Risk mark failed for %s", symbol)
        halted = self.executor.get_account_snapshot().halted
        if halted is not None and not self.closing:
            await self._fail(f"paper ledger halted: {halted}")

    async def _on_price(self, event: Event) -> None:
        if (
            not isinstance(event, PriceEvent)
            or event.exchange_id != self.exchange_id
            or str(event.symbol) not in self.cfg.symbols
        ):
            return
        try:
            self.executor.set_mark(str(event.symbol), event.price)
        except Exception:
            logger.exception("Live mark rejected for %s", event.symbol)

    async def _on_signal(self, event: Event) -> None:
        try:
            if isinstance(event, ExitSignal):
                await self._exit(event)
            elif isinstance(event, EntrySignal):
                order = await self.entry_order(event)
                if order is not None:
                    await self.om.submit_order(order)
        except _HALT_ERRORS as exc:
            await self._fail(f"paper ledger halted: {exc}")
        except (RiskRejectedError, ValueError) as exc:
            logger.warning("Skipped order from signal in %s: %s", self.session.session_id, exc)
        except PersistenceUnavailableError as exc:
            logger.warning(
                "Entry refused in %s until fills persist: %s", self.session.session_id, exc
            )
        except Exception:
            logger.exception("Failed to submit order from signal in %s", self.session.session_id)
        await self._fail_if_halted()

    async def _exit(self, event: ExitSignal) -> None:
        order = await self.exit_order(event)
        if order is None:
            return
        symbol = str(order.symbol)
        if await self._fail_if_halted():
            return
        await self.protect.disarm(symbol)
        try:
            order_id = await self.om.submit_order(order)
        except BaseException:
            if not await self._fail_if_halted():
                await self.protect.rearm(symbol)
            raise
        if order_id is None or self.om.get_order_status(order_id) != OrderStatus.FILLED:
            await self.protect.rearm(symbol)

    async def _on_fill(self, event: Event) -> None:
        if not isinstance(event, OrderFillEvent):
            return
        symbol = str(event.symbol)
        position = None
        try:
            positions = [p for p in await self.executor.get_positions(symbol) if p.quantity > 0]
            position = positions[0] if positions else None
            self.context.set_position(symbol, position)
            self.context.set_portfolio_value(await self.executor.get_equity())
        except Exception:
            logger.exception("Position feedback failed for %s", self.session.session_id)
        for strategy in self.engine.get_all_strategies().values():
            try:
                await strategy.on_fill(event)
            except Exception as exc:
                logger.exception("Strategy %s on_fill failed", strategy.strategy_id)
                self.engine.errors += 1
                self.engine.last_error = f"{strategy.strategy_id}: {exc!r}"
        self._manual_orders.pop(event.order_id, None)
        if not self.closing:
            try:
                await self.protect.on_fill(event)
            except Exception as exc:
                logger.exception("Protective orders not updated for %s", self.session.session_id)
                self.protect.failures.append(f"{symbol}: protective update failed ({exc})")
            halted = await self._fail_if_halted()
            if not halted and position is not None and self.protect.armed(symbol) is None:
                await self._close_unprotected(symbol, position.quantity)
                held = await self.executor.get_positions(symbol)
                position = next((p for p in held if p.quantity > 0), None)
        self.session.last_fill_at = datetime.now(UTC)
        metrics.set_session_position(
            self.session.session_id,
            self.cfg.id,
            self.exchange_id,
            symbol,
            float(position.quantity) if position is not None else 0.0,
        )
        await self._broadcast(
            {
                "event": "order_fill",
                "session_id": self.session.session_id,
                "order_id": event.order_id,
                "symbol": symbol,
                "side": str(event.side),
                "quantity": str(event.quantity),
                "price": str(event.price),
                "fee": str(event.fee),
                "realized_pnl": None if event.realized_pnl is None else str(event.realized_pnl),
                "tag": event.tag,
                "mark_source": event.mark_source,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        try:
            await self.risk.on_fill(self.session.session_id, event)
        except Exception:
            logger.exception("Risk fill feedback failed for %s", self.session.session_id)

    async def _on_feed_status(self, event: Event) -> None:
        if not isinstance(event, FeedStatusEvent) or event.exchange_id != self.exchange_id:
            return
        if self.feed.is_live(self.exchange_id):
            if self.entries_paused is not None:
                logger.info("Feed live again: entries resumed for %s", self.session.session_id)
            self.entries_paused = None
            return
        self.executor.mark_feed_stale()
        reasons = self.feed.stale_reasons(self.exchange_id)
        self.entries_paused = "feed stale: " + ", ".join(
            f"{scope} {'/'.join(r)}" for scope, r in sorted(reasons.items())
        )
        logger.warning("Entries paused for %s (%s)", self.session.session_id, self.entries_paused)

    # ------------------------------------------------------------------
    # Signals -> orders
    # ------------------------------------------------------------------

    async def entry_order(self, event: EntrySignal) -> OrderRequest | None:
        """An opening order, or None: wrong symbol, spot short, paused, held, or no size."""
        symbol = str(event.symbol)
        if symbol not in self.cfg.symbols or self.closing:
            return None
        if event.direction == Direction.SHORT and self.market_type == MarketType.SPOT:
            await self._spot_short_ignored("entry_short", symbol)
            return None
        if self.entries_paused is not None:
            logger.info("Entry on %s skipped: %s", symbol, self.entries_paused)
            return None
        if await self._holding(symbol):
            return None
        fraction = Decimal(str(self.cfg.position_sizing.max_position_pct)) / _HUNDRED
        quantity = await self.risk.size_entry(self.session.session_id, symbol, fraction)
        if quantity is None:
            logger.info("Entry on %s skipped: no tradable size", symbol)
            return None
        mark = self.risk.mark(self.exchange_id, symbol)
        return OrderRequest(
            symbol=Symbol(symbol),
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=self.cfg.id,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
            stop_price=self.protect.planned_stop(symbol, mark) if mark is not None else None,
            tag=ENTRY_TAG,
        )

    async def exit_order(self, event: ExitSignal) -> OrderRequest | None:
        """A reduce-only SELL of ``close_pct`` of the long, floored to the lot step."""
        symbol = str(event.symbol)
        if symbol not in self.cfg.symbols:
            return None
        positions = [p for p in await self.executor.get_positions(symbol) if p.quantity > 0]
        if not positions:
            return None
        pos = positions[0]
        close_pct = Decimal(str(min(max(event.close_pct, 0.0), 1.0)))
        quantity = pos.quantity if close_pct == 1 else self._floor(symbol, pos.quantity * close_pct)
        if quantity <= 0:
            return None
        return OrderRequest(
            symbol=Symbol(symbol),
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=quantity,
            strategy_id=self.cfg.id,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
            reduce_only=True,
            tag=EXIT_TAG,
        )

    # ------------------------------------------------------------------
    # Manual orders (quick-trade, webhook)
    # ------------------------------------------------------------------

    async def submit_manual(
        self,
        symbol: str,
        side: Side,
        order_type: OrderType,
        quantity: Decimal | None,
        price: Decimal | None,
        protection: ProtectionSpec | None,
    ) -> ManualOrderResult:
        """A BUY opens (sized by size_entry when no quantity); a SELL only reduces the long."""
        if self.closing or self.session.status != "running":
            raise SessionNotRunningError(f"session {self.session.session_id} is not running")
        if symbol not in self.cfg.symbols:
            raise ValueError(f"{symbol} is not traded by session {self.session.session_id}")
        if side == Side.SELL:
            return await self._manual_exit(symbol, order_type, quantity, protection)
        if self.entries_paused is not None:
            raise ValueError(f"entries are paused ({self.entries_paused})")
        if quantity is None:
            fraction = Decimal(str(self.cfg.position_sizing.max_position_pct)) / _HUNDRED
            quantity = await self.risk.size_entry(self.session.session_id, symbol, fraction)
            if quantity is None:
                raise ValueError(f"no tradable entry size for {symbol}")
        if await self._fail_if_halted():
            raise PaperSessionHaltedError(f"session {self.session.session_id} ledger is halted")
        tag = f"{MANUAL_TAG_PREFIX}{uuid.uuid4()}"
        self.protect.register_manual(tag, protection)
        reference = price if price is not None else self.risk.mark(self.exchange_id, symbol)
        stop = None
        if protection is not None and protection.stop_loss is not None:
            stop = protection.stop_loss
        elif protection is not None and protection.trailing_pct is not None:
            stop = reference * (1 - protection.trailing_pct) if reference is not None else None
        elif reference is not None:
            stop = self.protect.planned_stop(symbol, reference)
        order = OrderRequest(
            symbol=Symbol(symbol),
            side=Side.BUY,
            order_type=order_type,
            quantity=quantity,
            strategy_id=self.cfg.id,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
            price=price,
            stop_price=stop,
            tag=tag,
        )
        try:
            order_id = await self.om.submit_order(order)
        except BaseException as exc:
            self.protect.discard_manual(tag)
            if isinstance(exc, _HALT_ERRORS):
                await self._fail(f"paper ledger halted: {exc}")
            raise
        status = self.om.get_order_status(order_id) if order_id is not None else None
        group = self.protect.armed(symbol)
        filled = status == OrderStatus.FILLED
        if order_id is not None and status not in (OrderStatus.FILLED, OrderStatus.CANCELLED):
            self._manual_orders[order_id] = tag
        fill = self._market_fill(order_id) if filled else None
        return ManualOrderResult(
            order_id=order_id,
            status=str(status) if status is not None else "NOOP",
            quantity=quantity,
            price=price,
            protection="armed" if filled and group is not None else "pending",
            protective_order_id=group.order_id if filled and group is not None else None,
            fill_price=fill.price if fill is not None else None,
            mark_source=fill.mark_source if fill is not None else None,
        )

    async def _manual_exit(
        self,
        symbol: str,
        order_type: OrderType,
        quantity: Decimal | None,
        protection: ProtectionSpec | None,
    ) -> ManualOrderResult:
        if protection is not None:
            raise ValueError("stop-loss / take-profit apply to entries only")
        if order_type != OrderType.MARKET:
            raise ValueError("manual exits are MARKET orders (a resting SELL would unprotect)")
        positions = [p for p in await self.executor.get_positions(symbol) if p.quantity > 0]
        if not positions:
            raise SpotShortError(f"no {symbol} held: a spot SELL cannot open a short")
        held = positions[0].quantity
        qty = held if quantity is None else min(quantity, held)
        order = OrderRequest(
            symbol=Symbol(symbol),
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=qty,
            strategy_id=self.cfg.id,
            exchange_id=self.exchange_id,
            market_type=self.market_type,
            reduce_only=True,
            tag=f"{MANUAL_TAG_PREFIX}exit",
        )
        if await self._fail_if_halted():
            raise PaperSessionHaltedError(f"session {self.session.session_id} ledger is halted")
        await self.protect.disarm(symbol)
        try:
            order_id = await self.om.submit_order(order)
        except BaseException:
            if not await self._fail_if_halted():
                await self.protect.rearm(symbol)
            raise
        status = self.om.get_order_status(order_id) if order_id is not None else None
        if status != OrderStatus.FILLED:
            await self.protect.rearm(symbol)
        group = self.protect.armed(symbol)
        fill = self._market_fill(order_id) if status == OrderStatus.FILLED else None
        return ManualOrderResult(
            order_id=order_id,
            status=str(status) if status is not None else "NOOP",
            quantity=qty,
            price=None,
            protection="armed" if group is not None else "none",
            protective_order_id=group.order_id if group is not None else None,
            fill_price=fill.price if fill is not None else None,
            mark_source=fill.mark_source if fill is not None else None,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _market_fill(self, order_id: str | None) -> PaperFill | None:
        return next(
            (f for f in reversed(self.executor.get_fills()) if f.order_id == order_id), None
        )

    async def _holding(self, symbol: str) -> bool:
        if any(p.quantity > 0 for p in await self.executor.get_positions(symbol)):
            return True
        return any(o.get("side") == "BUY" for o in await self.executor.fetch_open_orders(symbol))

    def _floor(self, symbol: str, quantity: Decimal) -> Decimal:
        try:
            rules = self.risk.market_rules(self.session.session_id).get(symbol)
        except KeyError:
            rules = None
        return rules.floor_qty(quantity) if rules is not None else quantity

    def _atr(self, symbol: str) -> Decimal | None:
        values = self.context.indicator(
            "atr", symbol, self.primary, period=self.cfg.risk.atr_period
        )
        if len(values) == 0:
            return None
        last = float(values[-1])
        return None if math.isnan(last) else Decimal(str(last))

    async def _fail_if_halted(self) -> bool:
        """A halted ledger takes no more orders: error, flatten and stop the session."""
        halted = self.executor.get_account_snapshot().halted
        if halted is None:
            return False
        await self._fail(f"paper ledger halted: {halted}")
        return True

    async def _close_unprotected(self, symbol: str, quantity: Decimal) -> None:
        """Decision 17: a position whose protective stop cannot be placed is closed."""
        reason = self.protect.failures[-1] if self.protect.failures else "no protective order"
        logger.error("Closing unprotected %s in %s: %s", symbol, self.session.session_id, reason)
        await self._broadcast(
            {
                "event": "protection_failed",
                "session_id": self.session.session_id,
                "symbol": symbol,
                "quantity": str(quantity),
                "reason": reason,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )
        try:
            await self.om.submit_order(
                OrderRequest(
                    symbol=Symbol(symbol),
                    side=Side.SELL,
                    order_type=OrderType.MARKET,
                    quantity=quantity,
                    strategy_id=self.cfg.id,
                    exchange_id=self.exchange_id,
                    market_type=self.market_type,
                    reduce_only=True,
                    tag=UNPROTECTED_TAG,
                )
            )
        except Exception as exc:
            logger.warning("Reduce-only close of unprotected %s failed (%s)", symbol, exc)
            try:
                for fill in await self.executor.flatten(symbol, reason=UNPROTECTED_TAG):
                    await self.om.on_external_fill(fill)
            except Exception as flatten_exc:
                await self._fail(f"{symbol} is unprotected and could not be closed: {flatten_exc}")

    async def _on_cancel(self, event: Event) -> None:
        if isinstance(event, OrderCancelEvent):
            await self._release_manual(event.order_id, event.reason or "cancelled")

    async def _release_manual(self, order_id: str, reason: str) -> None:
        """A manual entry that will never fill: drop its protection spec and tell the client."""
        tag = self._manual_orders.pop(order_id, None)
        if tag is None:
            return
        self.protect.discard_manual(tag)
        await self._broadcast(
            {
                "event": "order_cancel",
                "session_id": self.session.session_id,
                "order_id": order_id,
                "reason": reason,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    async def _spot_short_ignored(self, signal_type: str, symbol: str) -> None:
        self.spot_shorts_ignored += 1
        metrics.record_spot_short_ignored(self.cfg.id, signal_type)
        logger.warning(
            "spot_short_ignored: %s %s dropped in spot session %s",
            signal_type,
            symbol,
            self.session.session_id,
        )
        await self._broadcast(
            {
                "event": "spot_short_ignored",
                "session_id": self.session.session_id,
                "symbol": symbol,
                "signal_type": signal_type,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    async def _broadcast(self, payload: dict[str, Any]) -> None:
        try:
            from hydra.core.websocket import manager

            await manager.broadcast(f"trades:{self.session.session_id}", payload)
        except Exception:
            logger.debug("Could not broadcast to WebSocket", exc_info=True)

    async def broadcast_status(self) -> None:
        s = self.session
        await self._broadcast(
            {
                "event": "session_status",
                "session_id": s.session_id,
                "status": s.status,
                "positions_remaining": s.positions_at_stop,
                "flatten_incomplete": s.flatten_incomplete,
                "error_message": s.error_message,
                "timestamp": datetime.now(UTC).isoformat(),
            }
        )

    async def _fail(self, reason: str) -> None:
        """Hand the failure to the manager in its own task (it stops this session's feed)."""
        if self.closing:
            return
        self.closing = True
        task: asyncio.Task[None] = asyncio.create_task(self._on_failure(self.session, reason))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)


_BACKGROUND: set[asyncio.Task[None]] = set()
