"""Grid trading bot strategy.

Places buy limit orders at evenly-spaced price levels below the current price
and sell limit orders above it.  When a buy fills, a sell is placed one level
up; when a sell fills, a buy is placed one level down.  The profit on each
complete buy→sell cycle equals roughly one grid step multiplied by the order
quantity (minus fees).

Supports two spacing modes:

- **arithmetic** — fixed absolute distance between levels
  (``step = (upper - lower) / grid_count``)
- **geometric** — fixed percentage distance between levels
  (``ratio = (upper / lower) ** (1 / grid_count)``)

Configuration keys (``parameters`` dict in ``StrategyConfig``):

    upper_price      float  — grid upper bound
    lower_price      float  — grid lower bound
    grid_count       int    — number of intervals (levels = grid_count + 1)
    total_investment float  — total capital in quote currency
    grid_type        str    — "arithmetic" (default) or "geometric"
"""

from __future__ import annotations

import logging
from decimal import ROUND_DOWN, Decimal

from hydra.core.events import BarEvent, OrderFillEvent
from hydra.core.types import OrderType, Side
from hydra.strategy.base import OrderAction, OrderManagementStrategy, PlaceOrder
from hydra.strategy.config import StrategyConfig
from hydra.strategy.context import StrategyContext

logger = logging.getLogger(__name__)

# Decimal precision sentinel used for rounding quantities.
_QUANTITY_PRECISION = Decimal("0.00000001")  # 8 decimal places (BTC-style)


class GridStrategy(OrderManagementStrategy):
    """Order-management strategy that implements a grid trading bot.

    The strategy is entirely passive after initialisation: all order
    placements happen reactively in :meth:`on_start` and :meth:`on_fill`.
    :meth:`on_bar` is used only for boundary-violation monitoring.
    """

    def __init__(self, config: StrategyConfig, context: StrategyContext) -> None:
        super().__init__(config, context)

        # --- Computed grid state ---
        self._grid_levels: list[Decimal] = []
        self._quantity_per_grid: Decimal = Decimal("0")

        # --- Order tracking (level_index → exchange order_id) ---
        self._buy_orders: dict[int, str] = {}
        self._sell_orders: dict[int, str] = {}

        # --- Runtime state ---
        self._initialized: bool = False
        self._grid_profit: Decimal = Decimal("0")
        self._cycles_completed: int = 0

        # Index of the last buy level filled (per level), used to link
        # a sell-fill back to its originating buy price for P&L accounting.
        # Maps sell_level_index → buy_price
        self._pending_sell_buy_price: dict[int, Decimal] = {}

    # -----------------------------------------------------------------------
    # Properties
    # -----------------------------------------------------------------------

    @property
    def required_history(self) -> int:
        """Grid bots need no historical bars to operate."""
        return 0

    @property
    def grid_profit(self) -> Decimal:
        """Cumulative realised profit from completed buy→sell cycles."""
        return self._grid_profit

    @property
    def cycles_completed(self) -> int:
        """Number of fully completed buy→sell cycles."""
        return self._cycles_completed

    # -----------------------------------------------------------------------
    # Grid initialisation helpers
    # -----------------------------------------------------------------------

    def _parse_params(self) -> tuple[Decimal, Decimal, int, Decimal, str]:
        """Extract and validate grid parameters from strategy config."""
        params = self._config.parameters
        upper = Decimal(str(params["upper_price"]))
        lower = Decimal(str(params["lower_price"]))
        grid_count = int(params["grid_count"])
        total_investment = Decimal(str(params["total_investment"]))
        grid_type = str(params.get("grid_type", "arithmetic")).lower()

        if upper <= lower:
            msg = f"upper_price ({upper}) must be greater than lower_price ({lower})"
            raise ValueError(msg)
        if grid_count < 2:
            msg = f"grid_count must be at least 2, got {grid_count}"
            raise ValueError(msg)
        if total_investment <= 0:
            msg = f"total_investment must be positive, got {total_investment}"
            raise ValueError(msg)
        if grid_type not in ("arithmetic", "geometric"):
            msg = f"grid_type must be 'arithmetic' or 'geometric', got {grid_type!r}"
            raise ValueError(msg)

        return upper, lower, grid_count, total_investment, grid_type

    def _compute_levels(
        self,
        upper: Decimal,
        lower: Decimal,
        grid_count: int,
        grid_type: str,
    ) -> list[Decimal]:
        """Return the sorted list of price levels (length = grid_count + 1)."""
        levels: list[Decimal] = []
        if grid_type == "arithmetic":
            step = (upper - lower) / Decimal(grid_count)
            for i in range(grid_count + 1):
                levels.append(lower + step * Decimal(i))
        else:
            # geometric: levels spaced by equal percentage
            ratio = (upper / lower) ** (Decimal(1) / Decimal(grid_count))
            current = lower
            for i in range(grid_count + 1):
                levels.append(lower * ratio ** Decimal(i))
                current = current  # keep pyright happy
            _ = current
        return levels

    def _compute_quantity_per_grid(
        self,
        upper: Decimal,
        lower: Decimal,
        grid_count: int,
        total_investment: Decimal,
    ) -> Decimal:
        """Approximate quantity per grid level using mid-price as denominator."""
        mid_price = (upper + lower) / Decimal(2)
        raw = total_investment / Decimal(grid_count) / mid_price
        return raw.quantize(_QUANTITY_PRECISION, rounding=ROUND_DOWN)

    def _tag(self, side: str, level_index: int) -> str:
        """Return a consistent tag string for an order at *level_index*."""
        return f"grid_{side.lower()}_{level_index}"

    def _symbol(self) -> str:
        """Return the first configured symbol (grid bots are single-symbol)."""
        symbols = self._config.symbols
        if not symbols:
            msg = "GridStrategy requires at least one symbol in config.symbols"
            raise ValueError(msg)
        return symbols[0]

    # -----------------------------------------------------------------------
    # on_start — place the initial grid orders
    # -----------------------------------------------------------------------

    async def on_start(self) -> list[OrderAction]:
        """Compute the grid and place all initial limit orders.

        Orders below the current price are limit buys; orders above are
        limit sells.  The level closest to current price is skipped (no
        order placed) to avoid an immediately-fillable order crossing the
        spread.
        """
        upper, lower, grid_count, total_investment, grid_type = self._parse_params()
        symbol = self._symbol()

        self._grid_levels = self._compute_levels(upper, lower, grid_count, grid_type)
        self._quantity_per_grid = self._compute_quantity_per_grid(
            upper, lower, grid_count, total_investment
        )

        # Determine current price from latest bar; fall back to mid-price if
        # no bar is available yet (e.g. cold start without data).
        latest = self._context.latest_bar(symbol, self._config.timeframes.primary)
        if latest is not None:
            current_price = latest.close
        else:
            current_price = (upper + lower) / Decimal(2)
            logger.warning(
                "GridStrategy %s: no bar data available; using mid-price %s as current price",
                self.strategy_id,
                current_price,
            )

        # Detect if current price is outside the grid range
        if current_price < lower:
            logger.warning(
                "GridStrategy %s: current price %s is below lower bound %s",
                self.strategy_id,
                current_price,
                lower,
            )
        elif current_price > upper:
            logger.warning(
                "GridStrategy %s: current price %s is above upper bound %s",
                self.strategy_id,
                current_price,
                upper,
            )

        actions: list[OrderAction] = []

        for idx, level in enumerate(self._grid_levels):
            if level < current_price:
                # Buy limit below current price
                tag = self._tag("buy", idx)
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.BUY,
                        order_type=OrderType.LIMIT,
                        quantity=self._quantity_per_grid,
                        price=level,
                        tag=tag,
                    )
                )
                # We don't have exchange order IDs here; the executor will
                # assign them and update our tracking via on_fill.
            elif level > current_price:
                # Sell limit above current price
                tag = self._tag("sell", idx)
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.SELL,
                        order_type=OrderType.LIMIT,
                        quantity=self._quantity_per_grid,
                        price=level,
                        tag=tag,
                    )
                )
            else:
                # level == current_price exactly: skip to avoid crossing spread
                logger.debug(
                    "GridStrategy %s: skipping level %d at %s (equals current price)",
                    self.strategy_id,
                    idx,
                    level,
                )

        self._initialized = True
        logger.info(
            "GridStrategy %s: initialised with %d levels (%d actions), "
            "qty_per_grid=%s, current_price=%s",
            self.strategy_id,
            len(self._grid_levels),
            len(actions),
            self._quantity_per_grid,
            current_price,
        )
        return actions

    # -----------------------------------------------------------------------
    # on_bar — boundary monitoring only
    # -----------------------------------------------------------------------

    async def on_bar(self, bar: BarEvent) -> list[OrderAction]:
        """Monitor grid boundary violations; returns no order actions."""
        if not self._initialized or not self._grid_levels:
            return []

        current_price = bar.ohlcv.close if bar.ohlcv else None
        if current_price is None:
            return []

        lower = self._grid_levels[0]
        upper = self._grid_levels[-1]

        if current_price < lower:
            logger.warning(
                "GridStrategy %s: price %s broke below lower grid bound %s",
                self.strategy_id,
                current_price,
                lower,
            )
        elif current_price > upper:
            logger.warning(
                "GridStrategy %s: price %s broke above upper grid bound %s",
                self.strategy_id,
                current_price,
                upper,
            )

        return []

    # -----------------------------------------------------------------------
    # on_fill — the core reactive logic
    # -----------------------------------------------------------------------

    async def on_fill(self, fill: OrderFillEvent) -> list[OrderAction]:
        """React to an order fill by placing the reciprocal order.

        - BUY fill at level N  → place SELL at level N+1
        - SELL fill at level N → place BUY at level N-1, record profit
        """
        if not self._initialized:
            return []

        tag = ""
        # The executor sets the tag on the fill event via params or the tag
        # field.  We rely on the fill's order_id to look up our tracking, but
        # since the executor must relay the tag, we parse it from the order_id
        # or from a convention.  Here we read from the tag stored in tracking.
        #
        # Strategy: scan buy/sell order maps for the incoming order_id.
        order_id = fill.order_id
        symbol = self._symbol()
        actions: list[OrderAction] = []

        # --- Check if it was a tracked BUY ---
        buy_level_idx: int | None = None
        for idx, oid in self._buy_orders.items():
            if oid == order_id:
                buy_level_idx = idx
                break

        if buy_level_idx is not None:
            del self._buy_orders[buy_level_idx]
            next_idx = buy_level_idx + 1

            if next_idx < len(self._grid_levels):
                sell_price = self._grid_levels[next_idx]
                sell_tag = self._tag("sell", next_idx)
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.SELL,
                        order_type=OrderType.LIMIT,
                        quantity=self._quantity_per_grid,
                        price=sell_price,
                        tag=sell_tag,
                    )
                )
                # Remember the buy price so we can compute profit when sell fills
                self._pending_sell_buy_price[next_idx] = fill.price
                logger.debug(
                    "GridStrategy %s: BUY filled at level %d (price=%s), "
                    "placing SELL at level %d (price=%s)",
                    self.strategy_id,
                    buy_level_idx,
                    fill.price,
                    next_idx,
                    sell_price,
                )
            else:
                logger.warning(
                    "GridStrategy %s: BUY filled at top level %d — no higher sell level",
                    self.strategy_id,
                    buy_level_idx,
                )
            return actions

        # --- Check if it was a tracked SELL ---
        sell_level_idx: int | None = None
        for idx, oid in self._sell_orders.items():
            if oid == order_id:
                sell_level_idx = idx
                break

        if sell_level_idx is not None:
            del self._sell_orders[sell_level_idx]
            prev_idx = sell_level_idx - 1

            # Record profit if we know the originating buy price
            buy_price = self._pending_sell_buy_price.pop(sell_level_idx, None)
            if buy_price is not None:
                cycle_profit = (fill.price - buy_price) * self._quantity_per_grid
                self._grid_profit += cycle_profit
                self._cycles_completed += 1
                logger.info(
                    "GridStrategy %s: completed cycle at levels %d→%d, "
                    "profit=%s, total_profit=%s, cycles=%d",
                    self.strategy_id,
                    prev_idx,
                    sell_level_idx,
                    cycle_profit,
                    self._grid_profit,
                    self._cycles_completed,
                )

            if prev_idx >= 0:
                buy_price_new = self._grid_levels[prev_idx]
                buy_tag = self._tag("buy", prev_idx)
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.BUY,
                        order_type=OrderType.LIMIT,
                        quantity=self._quantity_per_grid,
                        price=buy_price_new,
                        tag=buy_tag,
                    )
                )
                logger.debug(
                    "GridStrategy %s: SELL filled at level %d (price=%s), "
                    "placing BUY at level %d (price=%s)",
                    self.strategy_id,
                    sell_level_idx,
                    fill.price,
                    prev_idx,
                    buy_price_new,
                )
            else:
                logger.warning(
                    "GridStrategy %s: SELL filled at bottom level %d — no lower buy level",
                    self.strategy_id,
                    sell_level_idx,
                )
            return actions

        # The fill belongs to an order we are not currently tracking.
        # This can happen for orders placed before the strategy was restarted.
        # Fall through and attempt tag-based recovery.
        tag = _extract_tag_from_fill(fill)
        if tag:
            return await self._handle_fill_by_tag(tag, fill, symbol)

        logger.debug(
            "GridStrategy %s: received unrecognised fill for order_id=%s",
            self.strategy_id,
            order_id,
        )
        return []

    async def _handle_fill_by_tag(
        self, tag: str, fill: OrderFillEvent, symbol: str
    ) -> list[OrderAction]:
        """Recover fill handling using the order tag (e.g. 'grid_buy_5')."""
        parts = tag.split("_")
        if len(parts) != 3 or parts[0] != "grid":
            return []

        side_str = parts[1]
        try:
            level_idx = int(parts[2])
        except ValueError:
            return []

        actions: list[OrderAction] = []
        if side_str == "buy":
            next_idx = level_idx + 1
            if next_idx < len(self._grid_levels):
                sell_price = self._grid_levels[next_idx]
                self._pending_sell_buy_price[next_idx] = fill.price
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.SELL,
                        order_type=OrderType.LIMIT,
                        quantity=self._quantity_per_grid,
                        price=sell_price,
                        tag=self._tag("sell", next_idx),
                    )
                )
        elif side_str == "sell":
            buy_price = self._pending_sell_buy_price.pop(level_idx, None)
            if buy_price is not None:
                cycle_profit = (fill.price - buy_price) * self._quantity_per_grid
                self._grid_profit += cycle_profit
                self._cycles_completed += 1

            prev_idx = level_idx - 1
            if prev_idx >= 0:
                actions.append(
                    PlaceOrder(
                        symbol=symbol,
                        side=Side.BUY,
                        order_type=OrderType.LIMIT,
                        quantity=self._quantity_per_grid,
                        price=self._grid_levels[prev_idx],
                        tag=self._tag("buy", prev_idx),
                    )
                )

        return actions

    # -----------------------------------------------------------------------
    # on_stop
    # -----------------------------------------------------------------------

    async def on_stop(self) -> list[OrderAction]:
        """Log grid summary on stop; cleanup orders handled by the executor."""
        logger.info(
            "GridStrategy %s stopping — cycles=%d, total_profit=%s",
            self.strategy_id,
            self._cycles_completed,
            self._grid_profit,
        )
        return []

    # -----------------------------------------------------------------------
    # Public state accessors (useful for tests and monitoring)
    # -----------------------------------------------------------------------

    def register_order(self, level_index: int, side: str, order_id: str) -> None:
        """Register an exchange order ID for a grid level.

        Called by the executor after an order has been acknowledged by the
        exchange so that :meth:`on_fill` can look up which grid level a fill
        belongs to.
        """
        if side.upper() == Side.BUY:
            self._buy_orders[level_index] = order_id
        else:
            self._sell_orders[level_index] = order_id


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _extract_tag_from_fill(fill: OrderFillEvent) -> str:
    """Extract a strategy tag from a fill event, if available."""
    # The executor may attach extra data via params; use a convention.
    # If nothing is available, return an empty string.
    return ""


def compute_arithmetic_levels(lower: Decimal, upper: Decimal, grid_count: int) -> list[Decimal]:
    """Return arithmetic grid levels (public helper, useful in tests)."""
    step = (upper - lower) / Decimal(grid_count)
    return [lower + step * Decimal(i) for i in range(grid_count + 1)]


def compute_geometric_levels(lower: Decimal, upper: Decimal, grid_count: int) -> list[Decimal]:
    """Return geometric grid levels (public helper, useful in tests)."""
    ratio = (upper / lower) ** (Decimal(1) / Decimal(grid_count))
    return [lower * ratio ** Decimal(i) for i in range(grid_count + 1)]
