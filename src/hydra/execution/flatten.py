"""Close a session's positions through its OrderManager and report what is left."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hydra.core.types import Direction, OrderRequest, OrderType, Side, Symbol

if TYPE_CHECKING:
    from hydra.execution.session_wiring import SessionRuntime

logger = logging.getLogger(__name__)

FLATTEN_TAG = "flatten"


@dataclass(frozen=True, slots=True)
class FlattenResult:
    positions_remaining: int
    failures: tuple[str, ...] = ()


async def flatten_session(runtime: SessionRuntime, reason: str = FLATTEN_TAG) -> FlattenResult:
    """Cancel every resting order of the session, sell every holding reduce-only, re-check.

    Each SELL goes through the OrderManager (fill event, risk, WS, metrics). If it cannot (no
    mark, halted ledger), the executor's own ``flatten`` exits and its fills are published.
    """
    session = runtime.session
    executor = runtime.executor
    om = runtime.om
    failures: list[str] = []
    runtime.closing = True

    await runtime.protect.clear()
    for symbol in session.symbols:
        try:
            for order in await executor.fetch_open_orders(symbol):
                order_id = str(order.get("oco_id") or order["id"])
                if not await om.cancel_order(order_id):
                    await executor.cancel_order(order_id, symbol)
        except Exception as exc:
            logger.exception(
                "Cancelling open orders of %s in %s failed", symbol, session.session_id
            )
            failures.append(f"{symbol}: cancel failed ({exc})")

    for symbol in session.symbols:
        try:
            positions = [p for p in await executor.get_positions(symbol) if p.quantity > 0]
        except Exception as exc:
            failures.append(f"{symbol}: position read failed ({exc})")
            continue
        for pos in positions:
            side = Side.SELL if pos.direction == Direction.LONG else Side.BUY
            try:
                await om.submit_order(
                    OrderRequest(
                        symbol=Symbol(symbol),
                        side=side,
                        order_type=OrderType.MARKET,
                        quantity=pos.quantity,
                        strategy_id=session.strategy_id,
                        exchange_id=pos.exchange_id,
                        market_type=runtime.market_type,
                        reduce_only=True,
                        tag=reason,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "Reduce-only flatten of %s in %s failed (%s); using the executor's flatten",
                    symbol,
                    session.session_id,
                    exc,
                )
                await _executor_flatten(runtime, symbol, failures)

    # A live executor's sells fill on the exchange, not in the local ledger: drain the
    # landed fills now so positions_at_stop below reports the ledger's truth, not the
    # pre-fill state (the poll loop owns fills; paper executors have no collector).
    collect = getattr(executor, "collect_exchange_fills", None)
    if collect is not None:
        try:
            for fill in await collect(list(session.symbols)):
                await om.on_external_fill(fill)
        except Exception as exc:
            logger.warning("Draining live fills during flatten of %s failed: %s", reason, exc)

    remaining = 0
    for symbol in session.symbols:
        try:
            remaining += sum(1 for p in await executor.get_positions(symbol) if p.quantity > 0)
        except Exception as exc:
            failures.append(f"{symbol}: re-check failed ({exc})")
            remaining += 1
    session.positions_at_stop = remaining
    session.flatten_incomplete = remaining > 0
    if remaining:
        logger.error(
            "Session %s still holds %d position(s) after flatten: %s",
            session.session_id,
            remaining,
            "; ".join(failures) or "no error reported",
        )
    return FlattenResult(positions_remaining=remaining, failures=tuple(failures))


async def _executor_flatten(runtime: SessionRuntime, symbol: str, failures: list[str]) -> None:
    flatten = getattr(runtime.executor, "flatten", None)
    if flatten is None:
        failures.append(f"{symbol}: no executor flatten")
        return
    try:
        fills = await flatten(symbol, reason=FLATTEN_TAG)
    except Exception as exc:
        logger.exception("Executor flatten of %s failed", symbol)
        failures.append(f"{symbol}: {exc}")
        return
    for fill in fills:
        await runtime.om.on_external_fill(fill)
