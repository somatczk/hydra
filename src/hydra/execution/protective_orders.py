"""Mandatory protective exits for spot positions (decision 17).

Every entry fill arms one protective group per symbol for the whole position: a STOP_MARKET, an
OCO (stop + take-profit) when a take-profit is set, or a TRAILING_STOP. Levels come from a
manual ``ProtectionSpec`` (quick-trade) or from the strategy's ``risk`` block, which defaults to a
5 % stop. Stops and OCOs go through the OrderManager as ``reduce_only`` orders tagged
``protective`` (exempt from stale-order cleanup). Before any other exit the group is disarmed,
because a resting stop locks the base the exit would sell.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any

from hydra.core.events import OrderFillEvent
from hydra.core.types import (
    Direction,
    ExchangeId,
    MarketType,
    OrderRequest,
    OrderType,
    Position,
    Side,
    Symbol,
)
from hydra.execution.order_manager import PROTECTIVE_TAG, OrderManager
from hydra.strategy.config import DEFAULT_STOP_LOSS_PCT, StrategyRiskConfig

logger = logging.getLogger(__name__)

_HUNDRED = Decimal("100")
_ONE = Decimal("1")
DEFAULT_STOP_FRACTION = Decimal(str(DEFAULT_STOP_LOSS_PCT)) / _HUNDRED


@dataclass(frozen=True, slots=True)
class ProtectionSpec:
    """Absolute exit levels for one position; ``trailing_pct`` is a fraction.

    A SHORT position inverts the geometry: its stop-loss sits above the entry and its
    take-profit below (the validator enforces the side's ordering).
    """

    stop_loss: Decimal | None = None
    take_profit: Decimal | None = None
    trailing_pct: Decimal | None = None
    direction: Direction = Direction.LONG

    def __post_init__(self) -> None:
        if self.trailing_pct is not None and self.stop_loss is not None:
            raise ValueError("a trailing stop and a fixed stop-loss are mutually exclusive")
        if self.trailing_pct is not None and self.take_profit is not None:
            raise ValueError("a trailing stop cannot be combined with a take-profit")
        if self.trailing_pct is not None and not 0 < self.trailing_pct < 1:
            raise ValueError(f"trailing_pct must be in (0, 1), got {self.trailing_pct}")
        for name, level in (("stop_loss", self.stop_loss), ("take_profit", self.take_profit)):
            if level is not None and level <= 0:
                raise ValueError(f"{name} must be > 0, got {level}")
        if self.stop_loss is None or self.take_profit is None:
            return
        if self.direction == Direction.LONG:
            if self.stop_loss >= self.take_profit:
                raise ValueError("stop_loss must be below take_profit for a long position")
        elif self.stop_loss <= self.take_profit:
            raise ValueError("stop_loss must be above take_profit for a short position")


def default_stop(entry: Decimal, direction: Direction = Direction.LONG) -> ProtectionSpec:
    """The 5 % catastrophe stop every position falls back to."""
    if direction == Direction.SHORT:
        return ProtectionSpec(stop_loss=entry * (_ONE + DEFAULT_STOP_FRACTION), direction=direction)
    return ProtectionSpec(stop_loss=entry * (_ONE - DEFAULT_STOP_FRACTION), direction=direction)


def spec_from_policy(
    policy: StrategyRiskConfig,
    entry: Decimal,
    atr: Decimal | None,
    direction: Direction = Direction.LONG,
) -> ProtectionSpec:
    """Absolute levels for a position entered at *entry*; an ATR policy without ATR
    uses the default."""
    short = direction == Direction.SHORT
    value = Decimal(str(policy.stop_loss_value))
    if policy.stop_loss_method == "trailing_stop":
        return ProtectionSpec(trailing_pct=value / _HUNDRED, direction=direction)
    if policy.stop_loss_method == "atr":
        if atr is None or atr <= 0:
            logger.warning(
                "ATR unavailable for the stop; using the %s%% default", DEFAULT_STOP_LOSS_PCT
            )
            return default_stop(entry, direction)
        stop = entry + value * atr if short else entry - value * atr
    else:
        stop = entry * (_ONE + value / _HUNDRED) if short else entry * (_ONE - value / _HUNDRED)
    if stop <= 0:
        return default_stop(entry, direction)
    take_profit: Decimal | None = None
    tp_value = Decimal(str(policy.take_profit_value))
    if policy.take_profit_method == "fixed_pct":
        take_profit = (
            entry * (_ONE - tp_value / _HUNDRED) if short else entry * (_ONE + tp_value / _HUNDRED)
        )
    elif policy.take_profit_method == "atr" and atr is not None and atr > 0:
        take_profit = entry - tp_value * atr if short else entry + tp_value * atr
    return ProtectionSpec(stop_loss=stop, take_profit=take_profit, direction=direction)


@dataclass(slots=True)
class ProtectiveGroup:
    """The resting protective order of one symbol (the OCO id for an OCO).

    ``leg_ids`` and ``confirmed`` are the live convention: a live create response carries
    the exchange legs (even a single-leg stop), and their presence is the exchange ack.
    Paper responses never do, so both stay empty/False there.
    """

    symbol: str
    order_id: str
    order_type: OrderType
    quantity: Decimal
    spec: ProtectionSpec
    leg_ids: tuple[str, ...] = ()
    confirmed: bool = False


class ProtectiveOrderManager:
    """Arms, disarms and re-arms the protective group of each symbol of one session."""

    def __init__(
        self,
        om: OrderManager,
        executor: Any,
        *,
        strategy_id: str,
        exchange_id: ExchangeId,
        market_type: MarketType,
        policy: StrategyRiskConfig,
        atr: Callable[[str], Decimal | None],
    ) -> None:
        self._om = om
        self._executor = executor
        self._strategy_id = strategy_id
        self._exchange_id: ExchangeId = exchange_id
        self._market_type = market_type
        self._policy = policy
        self._atr = atr
        self._groups: dict[str, ProtectiveGroup] = {}
        self._disarmed: dict[str, ProtectionSpec] = {}
        self._manual: dict[str, ProtectionSpec | None] = {}
        self.failures: list[str] = []

    # -- Queries ----------------------------------------------------------------

    def armed(self, symbol: str) -> ProtectiveGroup | None:
        return self._groups.get(symbol)

    def groups(self) -> dict[str, ProtectiveGroup]:
        return dict(self._groups)

    def is_protective(self, order_id: str) -> bool:
        return any(g.order_id == order_id for g in self._groups.values())

    def planned_stop(
        self, symbol: str, reference: Decimal, direction: Direction = Direction.LONG
    ) -> Decimal | None:
        """The stop an entry at *reference* would get (for the pre-trade risk_per_trade check)."""
        return self._policy_spec(symbol, reference, direction).stop_loss

    def _policy_spec(
        self, symbol: str, entry: Decimal, direction: Direction = Direction.LONG
    ) -> ProtectionSpec:
        return spec_from_policy(self._policy, entry, self._atr(symbol), direction)

    # -- Manual specs (quick-trade, webhook) -----------------------------------------

    def register_manual(self, tag: str, spec: ProtectionSpec | None) -> None:
        """Remember the levels for the entry submitted with *tag* (None: the strategy policy)."""
        self._manual[tag] = spec

    def discard_manual(self, tag: str) -> None:
        self._manual.pop(tag, None)

    # -- Fills -----------------------------------------------------------------------

    async def on_fill(self, event: OrderFillEvent) -> None:
        """Keep exactly one protective group sized to the open position after every fill.

        Direction-agnostic by construction: an entry fill carries its manual spec's tag
        (a short's entry is a SELL just as a long's is a BUY), an exit fill re-arms
        whatever the disarm stashed, and any fill that leaves a position re-sizes the
        group to it. Only an exit fill consumes the disarm stash — an entry never does
        (a stashed spec that survived a refused exit is stale for the new position).
        """
        symbol = str(event.symbol)
        position = await self._position(symbol)
        group = self._groups.get(symbol)
        if group is not None and event.order_id in (group.order_id, *group.leg_ids):
            del self._groups[symbol]
            if position is not None:
                await self._arm(position, group.spec)
            return
        manual = self._manual.pop(event.tag, None) if event.tag in self._manual else None
        exit_fill = position is None or (position.direction == Direction.LONG) != (
            event.side == Side.BUY
        )
        stash = self._disarmed.pop(symbol, None) if exit_fill else None
        spec = manual or stash or (group.spec if group is not None else None)
        if group is not None:
            await self._cancel(group)
        if position is not None:
            await self._arm(
                position,
                spec or self._policy_spec(symbol, position.avg_entry_price, position.direction),
            )

    # -- Exits -----------------------------------------------------------------------

    async def disarm(self, symbol: str) -> None:
        """Cancel the resting group before another exit sells the base it locks."""
        group = self._groups.get(symbol)
        if group is None:
            return
        await self._cancel(group)
        self._disarmed[symbol] = group.spec

    async def rearm(self, symbol: str) -> None:
        """Re-protect the position after an exit that did not happen."""
        spec = self._disarmed.pop(symbol, None)
        if symbol in self._groups:
            return
        position = await self._position(symbol)
        if position is not None:
            await self._arm(
                position,
                spec or self._policy_spec(symbol, position.avg_entry_price, position.direction),
            )

    async def clear(self) -> None:
        """Cancel every group (stop / flatten); nothing is re-armed afterwards."""
        for group in list(self._groups.values()):
            await self._cancel(group)
        self._disarmed.clear()
        self._manual.clear()

    # -- Recovery --------------------------------------------------------------------

    async def adopt(self, symbols: list[str]) -> None:
        """After a restore: take over the executor's resting SELL protection, arm any gap.

        A live executor's resting SELLs are adopted only when they carry hydra's own
        ``hyd-`` client id prefix (R5: the user's orders are never hijacked — a later disarm
        would cancel them); paper orders are all hydra's, so paper adopt is unchanged.
        """
        from hydra.execution.live_orders import CLIENT_ORDER_ID_PREFIX

        live = getattr(self._executor, "exchange_client", None) is not None
        for symbol in symbols:
            position = await self._position(symbol)
            if position is None:
                continue
            exit_side = "SELL" if position.direction == Direction.LONG else "BUY"
            resting = [
                o
                for o in await self._executor.fetch_open_orders(symbol)
                if str(o.get("side")) == exit_side
                and (
                    not live
                    or str(o.get("client_order_id") or "").startswith(CLIENT_ORDER_ID_PREFIX)
                )
            ]
            if resting:
                order = resting[0]
                group_id = str(order.get("oco_id") or order["id"])
                self._groups[symbol] = ProtectiveGroup(
                    symbol=symbol,
                    order_id=group_id,
                    order_type=OrderType.OCO if order.get("oco_id") else OrderType(order["type"]),
                    quantity=Decimal(str(order["amount"])),
                    spec=_spec_from_resting(resting),
                )
                continue
            await self._arm(
                position, self._policy_spec(symbol, position.avg_entry_price, position.direction)
            )

    # -- Internals -------------------------------------------------------------------

    async def _position(self, symbol: str) -> Position | None:
        positions = await self._executor.get_positions(symbol)
        return next((p for p in positions if p.quantity > 0), None)

    async def _cancel(self, group: ProtectiveGroup) -> None:
        self._groups.pop(group.symbol, None)
        if await self._om.cancel_order(group.order_id):
            return
        try:
            await self._executor.cancel_order(group.order_id, group.symbol)
        except ValueError:
            logger.debug("protective order %s already gone", group.order_id)

    async def _arm(self, position: Position, spec: ProtectionSpec) -> None:
        """Arm *spec*; only the ladder's definitive exhaustion is recorded here.

        ``StopUnplaceableError`` (every rung definitively rejected, ``exchange_stops``)
        records a failure and leaves no group, so the wiring's armed-is-None check
        market-closes. Anything else re-raises — an unknown-outcome network error or a
        breached stop propagates to the wiring's decision-17 market-close path instead
        of a second placement (R18: no double-stop).
        """
        # local import: exchange_stops imports this module at its own top level
        from hydra.execution.exchange_stops import StopUnplaceableError

        symbol = str(position.symbol)
        try:
            group = await self._place(position, spec)
        except StopUnplaceableError as exc:
            msg = f"{symbol}: protective order {spec} rejected ({exc})"
            logger.error("Protective order failed: %s", msg)
            self.failures.append(msg)
            return
        if group is not None:
            self._groups[symbol] = group

    async def _place(self, position: Position, spec: ProtectionSpec) -> ProtectiveGroup | None:
        symbol = str(position.symbol)
        qty = position.quantity
        side = Side.SELL if position.direction == Direction.LONG else Side.BUY
        if spec.direction != position.direction:
            spec = replace(spec, direction=position.direction)
        if spec.trailing_pct is not None:
            resp = await self._executor.create_order(
                symbol,
                str(side),
                OrderType.TRAILING_STOP,
                qty,
                params={
                    "trail_pct": str(spec.trailing_pct),
                    "tag": PROTECTIVE_TAG,
                    "reduce_only": True,
                },
            )
            return self._group_from_response(symbol, OrderType.TRAILING_STOP, qty, spec, resp)
        if spec.stop_loss is None:
            spec = replace(
                spec, stop_loss=default_stop(position.avg_entry_price, position.direction).stop_loss
            )
        order_type = OrderType.OCO if spec.take_profit is not None else OrderType.STOP_MARKET
        order_id = await self._om.submit_order(
            OrderRequest(
                symbol=Symbol(symbol),
                side=side,
                order_type=order_type,
                quantity=qty,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
                market_type=self._market_type,
                price=spec.take_profit,
                stop_price=spec.stop_loss,
                reduce_only=True,
                tag=PROTECTIVE_TAG,
            )
        )
        if order_id is None:
            return None
        return self._group_from_response(
            symbol, order_type, qty, spec, self._om.order_response(order_id) or {}, order_id
        )

    def _group_from_response(
        self,
        symbol: str,
        order_type: OrderType,
        qty: Decimal,
        spec: ProtectionSpec,
        resp: dict[str, Any],
        order_id: str | None = None,
    ) -> ProtectiveGroup:
        legs = tuple(str(leg["id"]) for leg in resp.get("legs") or [])
        return ProtectiveGroup(
            symbol=symbol,
            order_id=str(resp.get("id") or order_id),
            order_type=order_type,
            quantity=qty,
            spec=spec,
            leg_ids=legs,
            confirmed=bool(legs),
        )


def _spec_from_resting(orders: list[dict[str, Any]]) -> ProtectionSpec:
    stop = next((o.get("stop_price") for o in orders if o.get("stop_price") is not None), None)
    tp = next(
        (o.get("price") for o in orders if o.get("type") == "LIMIT" and o.get("oco_id")), None
    )
    trail = next((o.get("trail_pct") for o in orders if o.get("trail_pct") is not None), None)
    if trail is not None:
        return ProtectionSpec(trailing_pct=_dec(trail))
    return ProtectionSpec(stop_loss=_dec(stop), take_profit=_dec(tp))


def _dec(value: Any) -> Decimal | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    return Decimal(str(value))
