"""Dollar-Cost Averaging (DCA) bot strategy.

Implements a full DCA deal lifecycle:
  IDLE   -> base order placed -> ACTIVE
  ACTIVE -> safety orders placed as price drops, TP updated after each fill
  ACTIVE -> TP hit -> COMPLETED -> IDLE (reset for next deal)

The strategy manages its own order lifecycle via ``OrderManagementStrategy``
and returns explicit ``OrderAction`` objects from ``on_bar`` and ``on_fill``.
Safety orders are monitored every bar (bar-level deviation check) rather than
pre-placed, so they can be size-scaled and avoid exchange min-notional issues
on deeply out-of-the-money limit orders.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from enum import Enum, auto
from typing import Any

import numpy as np

from hydra.core.events import BarEvent, OrderFillEvent
from hydra.core.types import OrderType, Side
from hydra.strategy.base import CancelOrder, OrderAction, OrderManagementStrategy, PlaceOrder
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Deal state machine
# ---------------------------------------------------------------------------


class _DealState(Enum):
    IDLE = auto()
    ACTIVE = auto()
    COMPLETED = auto()


# ---------------------------------------------------------------------------
# Tag constants — used to identify fill events
# ---------------------------------------------------------------------------

_TAG_BASE = "base_order"
_TAG_SAFETY_PREFIX = "safety_"
_TAG_TP = "take_profit"

# ---------------------------------------------------------------------------
# Precision helpers
# ---------------------------------------------------------------------------

_HUNDRED = Decimal("100")
_ZERO = Decimal("0")
_ONE = Decimal("1")


def _d(value: float | int | str) -> Decimal:
    """Convert a value to Decimal via string to avoid float representation noise."""
    return Decimal(str(value))


class DCAStrategy(OrderManagementStrategy):
    """Dollar-Cost Averaging bot.

    Lifecycle
    ---------
    1. **IDLE** — waits for start condition (immediate or RSI oversold).
       Returns a market BUY (base order) when triggered.

    2. **ACTIVE** — after base order is filled, monitors price every bar.
       Places limit safety orders as price drops through successive deviation
       levels.  Updates a limit SELL (take-profit) after every fill.

    3. **COMPLETED** — once the TP is filled, resets all state back to IDLE.

    Parameters (from ``config.parameters``)
    ----------------------------------------
    base_order_size : float
        USDT amount for the initial market buy.  Default 100.
    safety_order_size : float
        USDT amount for the *first* safety order.  Default 50.
    safety_order_count : int
        Maximum number of safety orders per deal.  Default 5.
    price_deviation_pct : float
        % drop from average entry to trigger each successive safety order
        (applied cumulatively with ``step_scale``).  Default 1.0.
    volume_scale : float
        Multiplier applied to each successive safety order size.  Default 1.5.
    step_scale : float
        Multiplier applied to each successive deviation step.  Default 1.0
        (equal steps).
    take_profit_pct : float
        % above average entry at which to close the position.  Default 1.5.
    max_active_deals : int
        Maximum concurrent deals (currently only 1 is supported).  Default 1.
    start_condition : str
        ``"immediate"`` or ``"rsi_oversold"``.  Default ``"immediate"``.
    rsi_period : int
        RSI look-back when start_condition is ``"rsi_oversold"``.  Default 14.
    rsi_oversold : float
        RSI threshold for oversold condition.  Default 30.
    """

    # ------------------------------------------------------------------
    # Construction / initialisation
    # ------------------------------------------------------------------

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        super().__init__(config, context)
        self._reset_deal_state()

    def _reset_deal_state(self) -> None:
        """Return all deal-tracking variables to their IDLE defaults."""
        self._deal_state: _DealState = _DealState.IDLE
        self._base_order_filled: bool = False
        self._safety_orders_filled: int = 0
        self._safety_orders_placed: int = 0
        self._total_quantity: Decimal = _ZERO
        self._total_cost: Decimal = _ZERO
        self._avg_entry_price: Decimal = _ZERO
        # tag -> order_id for pending safety orders
        self._pending_safety_ids: dict[str, str] = {}
        self._pending_tp_id: str | None = None
        # Pre-computed deviation prices per safety order index (1-based)
        self._deviation_prices: list[Decimal] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def required_history(self) -> int:
        """DCA needs at least 1 bar; RSI condition needs more if enabled."""
        if self._start_condition == "rsi_oversold":
            return self._rsi_period + 1
        return 1

    # ------------------------------------------------------------------
    # Parameter accessors (read once from config, cached as properties)
    # ------------------------------------------------------------------

    @property
    def _params(self) -> dict[str, Any]:
        return self._config.parameters

    @property
    def _base_order_size(self) -> Decimal:
        return _d(self._params.get("base_order_size", 100))

    @property
    def _safety_order_size(self) -> Decimal:
        return _d(self._params.get("safety_order_size", 50))

    @property
    def _safety_order_count(self) -> int:
        return int(self._params.get("safety_order_count", 5))

    @property
    def _price_deviation_pct(self) -> Decimal:
        return _d(self._params.get("price_deviation_pct", 1.0))

    @property
    def _volume_scale(self) -> Decimal:
        return _d(self._params.get("volume_scale", 1.5))

    @property
    def _step_scale(self) -> Decimal:
        return _d(self._params.get("step_scale", 1.0))

    @property
    def _take_profit_pct(self) -> Decimal:
        return _d(self._params.get("take_profit_pct", 1.5))

    @property
    def _start_condition(self) -> str:
        return str(self._params.get("start_condition", "immediate"))

    @property
    def _rsi_period(self) -> int:
        return int(self._params.get("rsi_period", 14))

    @property
    def _rsi_oversold(self) -> float:
        return float(self._params.get("rsi_oversold", 30.0))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @property
    def _deal_active(self) -> bool:
        return self._deal_state == _DealState.ACTIVE

    def _safety_order_size_for(self, index: int) -> Decimal:
        """Return the USDT size for the *index*-th safety order (1-based).

        Each successive safety order is multiplied by ``volume_scale``::

            size_1 = safety_order_size
            size_2 = safety_order_size * volume_scale
            size_n = safety_order_size * volume_scale^(n-1)
        """
        return self._safety_order_size * (self._volume_scale ** (index - 1))

    def _cumulative_deviation_pct(self, index: int) -> Decimal:
        """Return the cumulative % deviation for the *index*-th safety order.

        With equal steps (``step_scale`` = 1.0)::

            deviation_1 = price_deviation_pct          (e.g. 1%)
            deviation_2 = price_deviation_pct * 2      (e.g. 2%)

        With scaling::

            step_1 = price_deviation_pct
            step_2 = price_deviation_pct * step_scale
            cumulative_n = sum(step_1 .. step_n)
        """
        cumulative = _ZERO
        step = self._price_deviation_pct
        for _ in range(index):
            cumulative += step
            step *= self._step_scale
        return cumulative

    def _compute_deviation_prices(self, avg_price: Decimal) -> list[Decimal]:
        """Compute the trigger price for each safety order level."""
        prices: list[Decimal] = []
        for i in range(1, self._safety_order_count + 1):
            cum_pct = self._cumulative_deviation_pct(i)
            trigger = avg_price * (_ONE - cum_pct / _HUNDRED)
            prices.append(trigger)
        return prices

    def _compute_tp_price(self) -> Decimal:
        """Compute the take-profit price from current average entry."""
        return self._avg_entry_price * (_ONE + self._take_profit_pct / _HUNDRED)

    def _update_avg_entry(self, fill_quantity: Decimal, fill_price: Decimal) -> None:
        """Update running totals and recalculate average entry price."""
        self._total_quantity += fill_quantity
        self._total_cost += fill_quantity * fill_price
        if self._total_quantity > _ZERO:
            self._avg_entry_price = self._total_cost / self._total_quantity

    def _tag_for_safety(self, index: int) -> str:
        return f"{_TAG_SAFETY_PREFIX}{index}"

    def _safety_index_from_tag(self, tag: str) -> int | None:
        """Extract the 1-based safety order index from a tag string."""
        if tag.startswith(_TAG_SAFETY_PREFIX):
            try:
                return int(tag[len(_TAG_SAFETY_PREFIX) :])
            except ValueError:
                return None
        return None

    def _is_rsi_oversold(self, bar: BarEvent) -> bool:
        """Return True when RSI is below the oversold threshold."""
        symbol = str(bar.symbol)
        tf = bar.timeframe
        bars = self._context.bars(symbol, tf, self._rsi_period + 1)
        if len(bars) < self._rsi_period + 1:
            return False
        close = np.array([float(b.close) for b in bars], dtype=np.float64)
        from hydra.indicators.library import rsi as _rsi

        rsi_values = _rsi(close, self._rsi_period)
        if len(rsi_values) == 0 or np.isnan(rsi_values[-1]):
            return False
        return float(rsi_values[-1]) < self._rsi_oversold

    # ------------------------------------------------------------------
    # Core interface
    # ------------------------------------------------------------------

    async def on_bar(self, bar: BarEvent) -> list[OrderAction]:
        """Process a bar and return any order actions required.

        Transitions:
        - IDLE + start condition met  → place base market order
        - ACTIVE + price at deviation → place next safety limit order
        - ACTIVE + price >= TP        → close position (market sell)
        """
        if bar.ohlcv is None:
            return []

        current_price: Decimal = bar.ohlcv.close
        symbol: str = str(bar.symbol)
        actions: list[OrderAction] = []

        # --- IDLE: check whether to start a new deal ---
        if self._deal_state == _DealState.IDLE:
            if self._should_start(bar):
                quantity = self._base_order_size / current_price
                logger.info(
                    "DCA deal starting",
                    extra={
                        "strategy_id": self.strategy_id,
                        "symbol": symbol,
                        "price": str(current_price),
                        "quantity": str(quantity),
                    },
                )
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.BUY,
                        order_type=OrderType.MARKET,
                        quantity=quantity,
                        tag=_TAG_BASE,
                    )
                )
                # Transition to ACTIVE immediately; fill will be confirmed via on_fill
                self._deal_state = _DealState.ACTIVE
            return actions

        # --- ACTIVE: monitor safety orders and TP ---
        if self._deal_state == _DealState.ACTIVE and self._base_order_filled:
            # Check if we should close the deal (TP hit at bar close)
            if self._total_quantity > _ZERO and current_price >= self._compute_tp_price():
                logger.info(
                    "DCA take-profit triggered on bar close",
                    extra={
                        "strategy_id": self.strategy_id,
                        "symbol": symbol,
                        "price": str(current_price),
                        "avg_entry": str(self._avg_entry_price),
                    },
                )
                # Cancel any pending safety orders before closing
                for tag, order_id in list(self._pending_safety_ids.items()):
                    actions.append(CancelOrder(order_id=order_id, symbol=symbol))
                    del self._pending_safety_ids[tag]
                # Market sell to close entire position
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.SELL,
                        order_type=OrderType.MARKET,
                        quantity=self._total_quantity,
                        tag=_TAG_TP,
                    )
                )
                return actions

            # Check whether to place the next safety order
            next_safety_index = self._safety_orders_placed + 1
            if next_safety_index <= self._safety_order_count and next_safety_index <= len(
                self._deviation_prices
            ):
                trigger_price = self._deviation_prices[next_safety_index - 1]
                tag = self._tag_for_safety(next_safety_index)
                if current_price <= trigger_price and tag not in self._pending_safety_ids:
                    so_size = self._safety_order_size_for(next_safety_index)
                    so_quantity = so_size / trigger_price
                    logger.info(
                        "DCA placing safety order",
                        extra={
                            "strategy_id": self.strategy_id,
                            "symbol": symbol,
                            "index": next_safety_index,
                            "trigger_price": str(trigger_price),
                            "quantity": str(so_quantity),
                        },
                    )
                    actions.append(
                        PlaceOrder(
                            symbol=symbol,
                            side=Side.BUY,
                            order_type=OrderType.LIMIT,
                            quantity=so_quantity,
                            price=trigger_price,
                            tag=tag,
                        )
                    )
                    self._safety_orders_placed += 1

        return actions

    async def on_fill(self, fill: OrderFillEvent) -> list[OrderAction]:
        """Handle an order fill and return follow-up actions.

        - Base order fill → update state, compute deviation prices, place TP.
        - Safety order fill → update avg entry, replace TP with new price.
        - TP fill → reset state to IDLE.
        """
        symbol = str(fill.symbol)
        # The session manager is expected to pass the strategy tag as order_id.
        # We match by checking tags stored in our tracking dicts.
        actions: list[OrderAction] = []

        # Identify what was filled by matching order_id against known pending IDs
        filled_tag = self._resolve_tag(fill)

        if filled_tag == _TAG_BASE:
            self._on_base_filled(fill)
            actions.extend(self._build_tp_order(symbol))
            logger.info(
                "DCA base order filled",
                extra={
                    "strategy_id": self.strategy_id,
                    "symbol": symbol,
                    "avg_entry": str(self._avg_entry_price),
                    "tp_price": str(self._compute_tp_price()),
                },
            )

        elif filled_tag == _TAG_TP:
            logger.info(
                "DCA take-profit filled — deal complete",
                extra={
                    "strategy_id": self.strategy_id,
                    "symbol": symbol,
                    "avg_entry": str(self._avg_entry_price),
                    "fill_price": str(fill.price),
                },
            )
            self._reset_deal_state()

        else:
            safety_index = self._safety_index_from_tag(filled_tag) if filled_tag else None
            if safety_index is not None:
                self._on_safety_filled(fill, safety_index)
                # Cancel old TP and replace with updated price
                if self._pending_tp_id is not None:
                    actions.append(CancelOrder(order_id=self._pending_tp_id, symbol=symbol))
                    self._pending_tp_id = None
                actions.extend(self._build_tp_order(symbol))
                logger.info(
                    "DCA safety order filled",
                    extra={
                        "strategy_id": self.strategy_id,
                        "symbol": symbol,
                        "safety_index": safety_index,
                        "new_avg_entry": str(self._avg_entry_price),
                        "new_tp_price": str(self._compute_tp_price()),
                    },
                )

        return actions

    async def on_stop(self) -> list[OrderAction]:
        """Cancel all pending orders and reset state on strategy stop."""
        actions: list[OrderAction] = []
        symbol = self._config.symbols[0] if self._config.symbols else ""

        for _tag, order_id in self._pending_safety_ids.items():
            actions.append(CancelOrder(order_id=order_id, symbol=symbol))

        if self._pending_tp_id is not None:
            actions.append(CancelOrder(order_id=self._pending_tp_id, symbol=symbol))

        self._reset_deal_state()
        return actions

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _should_start(self, bar: BarEvent) -> bool:
        """Return True when the configured start condition is satisfied."""
        if self._start_condition == "immediate":
            return True
        if self._start_condition == "rsi_oversold":
            return self._is_rsi_oversold(bar)
        # Unknown condition — log and default to False (safe)
        logger.warning(
            "Unknown DCA start_condition '%s', defaulting to no-start",
            self._start_condition,
        )
        return False

    def _on_base_filled(self, fill: OrderFillEvent) -> None:
        """Update state after base order fill."""
        self._base_order_filled = True
        self._update_avg_entry(fill.quantity, fill.price)
        # Pre-compute all deviation trigger prices from the initial avg entry
        self._deviation_prices = self._compute_deviation_prices(self._avg_entry_price)

    def _on_safety_filled(self, fill: OrderFillEvent, safety_index: int) -> None:
        """Update state after a safety order fill."""
        self._safety_orders_filled += 1
        self._update_avg_entry(fill.quantity, fill.price)
        # Remove from pending tracking
        tag = self._tag_for_safety(safety_index)
        self._pending_safety_ids.pop(tag, None)

    def _build_tp_order(self, symbol: str) -> list[OrderAction]:
        """Build a limit SELL order at the current TP price."""
        if self._total_quantity <= _ZERO:
            return []
        tp_price = self._compute_tp_price()
        action = PlaceOrder(
            symbol=symbol,
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=self._total_quantity,
            price=tp_price,
            tag=_TAG_TP,
        )
        return [action]

    def _resolve_tag(self, fill: OrderFillEvent) -> str | None:
        """Determine the logical tag for a fill event.

        The session manager is expected to echo the ``tag`` field back via
        ``fill.order_id``.  We also check our pending tracking structures as
        a fallback for implementations that use a generated order_id.
        """
        order_id = fill.order_id

        # Direct match against known pending IDs
        if self._pending_tp_id == order_id:
            return _TAG_TP

        for tag, pending_id in self._pending_safety_ids.items():
            if pending_id == order_id:
                return tag

        # Fall back to treating order_id as the tag directly
        known_tags = {_TAG_BASE, _TAG_TP}
        for i in range(1, self._safety_order_count + 1):
            known_tags.add(self._tag_for_safety(i))

        if order_id in known_tags:
            return order_id

        return None
