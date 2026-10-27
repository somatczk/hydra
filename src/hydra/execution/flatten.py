"""Close a session's positions through its OrderManager and report what is left."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from hydra.core.types import Direction, OrderRequest, OrderType, Side, Symbol
from hydra.execution.errors import SubmitUnknownError
from hydra.execution.paper_trading import (
    InsufficientBalanceError,
    LedgerInvariantError,
    OrderRejectedError,
    PaperSessionHaltedError,
)

if TYPE_CHECKING:
    from hydra.execution.session_wiring import SessionRuntime

logger = logging.getLogger(__name__)

FLATTEN_TAG = "flatten"
_DRAIN_SWEEPS = 3  # the bounded fill drain: ~2-3 sweeps / 10-15s at the 5 s poll interval


@dataclass(frozen=True, slots=True)
class FlattenResult:
    positions_remaining: int
    failures: tuple[str, ...] = ()


async def flatten_session(runtime: SessionRuntime, reason: str = FLATTEN_TAG) -> FlattenResult:
    """Cancel every resting order of the session, sell every holding reduce-only, re-check.

    Each SELL goes through the OrderManager (fill event, risk, WS, metrics). The executor's
    own ``flatten`` is a fallback ONLY for definitive pre-wire rejections — on an unknown
    submit outcome it must never fire, because the first sell may rest on the exchange and a
    second sell of the same lot spends money the session no longer owns (F4); the unknown is
    recorded, the drain books the landed sell if it made it, and whatever remains is reported
    honestly through ``positions_at_stop``.
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
            except (
                OrderRejectedError,
                InsufficientBalanceError,
                PaperSessionHaltedError,
                LedgerInvariantError,
            ) as exc:
                # pre-wire definitive refusals (a halted paper ledger's flatten is the closer)
                logger.warning(
                    "Reduce-only flatten of %s in %s failed (%s); using the executor's flatten",
                    symbol,
                    session.session_id,
                    exc,
                )
                await _executor_flatten(runtime, symbol, failures)
            except SubmitUnknownError as exc:
                # the sell may rest: never a second sell of the same lot (F4)
                logger.error(
                    "Reduce-only flatten of %s in %s has an unknown outcome (%s);"
                    " the position will be reported as remaining",
                    symbol,
                    session.session_id,
                    exc,
                )
                failures.append(f"{symbol}: flatten sell outcome unknown ({exc})")
            except Exception as exc:
                # not a definitive pre-wire rejection either: report, never re-sell (F4)
                logger.error(
                    "Reduce-only flatten of %s in %s failed (%s);"
                    " the position will be reported as remaining",
                    symbol,
                    session.session_id,
                    exc,
                )
                failures.append(f"{symbol}: flatten sell failed ({exc})")

    await _drain_live_fills(runtime)

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


async def _drain_live_fills(runtime: SessionRuntime) -> None:
    """A live executor's sells fill on the exchange, not in the local ledger: drain the
    landed fills, retrying within a bounded wait (F10), so positions_at_stop reports the
    ledger's truth rather than a REST-lagged pre-fill state."""
    executor = runtime.executor
    collect = getattr(executor, "collect_exchange_fills", None)
    if collect is None:
        return
    symbols = list(runtime.session.symbols)
    interval = float(getattr(executor, "poll_interval", 5.0) or 5.0)
    deadline = time.monotonic() + _DRAIN_SWEEPS * interval
    while True:
        try:
            for fill in await collect(symbols):
                await runtime.om.on_external_fill(fill)
        except Exception as exc:
            logger.warning("Draining live fills during flatten of %s failed: %s", FLATTEN_TAG, exc)
        if time.monotonic() >= deadline or not await _any_position(runtime, symbols):
            return
        await asyncio.sleep(min(interval, max(deadline - time.monotonic(), 0.0)))


async def _any_position(runtime: SessionRuntime, symbols: list[str]) -> bool:
    for symbol in symbols:
        try:
            if any(p.quantity > 0 for p in await runtime.executor.get_positions(symbol)):
                return True
        except Exception:
            return True  # unreadable: keep draining until the wait exhausts
    return False


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
