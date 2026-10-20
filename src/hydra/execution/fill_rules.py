"""Intrabar fill rules for resting orders evaluated against one closed OHLCV bar.

Pure functions, Decimal only, no I/O. The paper executor uses them; the backtester can adopt
them (``hydra.backtest`` sits above ``hydra.execution`` in the import layers).

Price path inside a bar: a bullish bar (close > open) is assumed to go open, low, high, close;
any other bar open, high, low, close. A level that the open has already crossed is touched at the
open (a gap), before anything else on the path. ``backtest/fills.py:_fill_oco`` picks its OCO leg
with ``oco_stop_first``, so paper and backtest share one rule.

Gap-through stops fill at the worse of the stop and the open; resting limits fill at the better
of the limit and the open (the backtester's rule). Slippage applies to stop and market fills only.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from hydra.core.types import OHLCV

Liquidity = Literal["maker", "taker"]
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class FillPrice:
    """Where a triggered order fills and whether it added or took liquidity."""

    price: Decimal
    liquidity: Liquidity


@dataclass(frozen=True, slots=True)
class StopLimitOutcome:
    """A triggered stop-limit either fills now or becomes a resting limit at ``limit``."""

    fill: FillPrice | None
    converted_to_limit: bool


@dataclass(frozen=True, slots=True)
class TrailingOutcome:
    """The trailing peak (or trough) after this bar and the fill, if the stop triggered."""

    extreme: Decimal
    fill: FillPrice | None


def _side(side: str) -> str:
    s = side.upper()
    if s not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    return s


def intrabar_path(bar: OHLCV) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """The assumed order of prices inside ``bar``."""
    if bar.close > bar.open:
        return (bar.open, bar.low, bar.high, bar.close)
    return (bar.open, bar.high, bar.low, bar.close)


def touch_index(bar: OHLCV, level: Decimal, *, falling: bool) -> int | None:
    """Index on ``intrabar_path`` where price first reaches ``level``; None if never.

    ``falling`` means the order waits for price to come down to the level (SELL stop, BUY limit).
    """
    path = intrabar_path(bar)
    for i, point in enumerate(path):
        if (falling and point <= level) or (not falling and point >= level):
            return i
    return None


def slipped(price: Decimal, side: str, slippage: Decimal) -> Decimal:
    """Move ``price`` against the trader by the fraction ``slippage``."""
    if _side(side) == "BUY":
        return price * (_ONE + slippage)
    return price * (_ONE - slippage)


def market_fill(side: str, reference: Decimal, slippage: Decimal) -> FillPrice:
    """Market order at the last closed-bar close, slipped; taker."""
    if reference <= 0:
        raise ValueError(f"reference price must be positive, got {reference}")
    return FillPrice(slipped(reference, side, slippage), "taker")


def limit_fill(side: str, limit: Decimal, bar: OHLCV) -> FillPrice | None:
    """Resting limit: BUY fills when low <= limit at min(limit, open), SELL mirrored; maker."""
    if _side(side) == "BUY":
        return FillPrice(min(limit, bar.open), "maker") if bar.low <= limit else None
    return FillPrice(max(limit, bar.open), "maker") if bar.high >= limit else None


def stop_trigger_price(side: str, stop: Decimal, bar: OHLCV) -> Decimal | None:
    """Price at which a stop triggers on this bar: the stop, or the open when it gapped through."""
    if _side(side) == "SELL":
        return min(stop, bar.open) if bar.low <= stop else None
    return max(stop, bar.open) if bar.high >= stop else None


def stop_market_fill(side: str, stop: Decimal, bar: OHLCV, slippage: Decimal) -> FillPrice | None:
    """Stop-market: fills at the worse of stop and open, slipped; taker."""
    trigger = stop_trigger_price(side, stop, bar)
    if trigger is None:
        return None
    return FillPrice(slipped(trigger, side, slippage), "taker")


def stop_limit_fill(
    side: str, stop: Decimal, limit: Decimal, bar: OHLCV
) -> StopLimitOutcome | None:
    """Stop-limit: None until the stop triggers.

    On trigger the limit order is placed. When the trigger price is already marketable against the
    limit it fills at the trigger (taker; the limit is only a cap). Otherwise it fills at the limit
    when the rest of the path after the trigger reaches it (maker), or converts to a resting limit.
    """
    s = _side(side)
    trigger = stop_trigger_price(s, stop, bar)
    if trigger is None:
        return None
    marketable = trigger >= limit if s == "SELL" else trigger <= limit
    if marketable:
        return StopLimitOutcome(FillPrice(trigger, "taker"), converted_to_limit=False)
    start = touch_index(bar, stop, falling=s == "SELL")
    rest = intrabar_path(bar)[(start or 0) + 1 :]
    reached = any(p >= limit for p in rest) if s == "SELL" else any(p <= limit for p in rest)
    if reached:
        return StopLimitOutcome(FillPrice(limit, "maker"), converted_to_limit=False)
    return StopLimitOutcome(None, converted_to_limit=True)


def oco_stop_first(side: str, stop: Decimal, take_profit: Decimal, bar: OHLCV) -> bool | None:
    """Which OCO leg fills on this bar: True = stop-loss, False = take-profit, None = neither.

    ``side`` is the side of both legs (SELL closes a long: stop below, take-profit above).
    When both levels are touched, the one reached first on ``intrabar_path`` wins.
    """
    s = _side(side)
    stop_at = touch_index(bar, stop, falling=s == "SELL")
    tp_at = touch_index(bar, take_profit, falling=s == "BUY")
    if stop_at is None and tp_at is None:
        return None
    if tp_at is None:
        return True
    if stop_at is None:
        return False
    return stop_at < tp_at


def oco_fill(
    side: str, stop: Decimal, take_profit: Decimal, bar: OHLCV, slippage: Decimal
) -> tuple[bool, FillPrice] | None:
    """The winning OCO leg (True = stop) and its fill; exactly one leg fills, or none."""
    stop_first = oco_stop_first(side, stop, take_profit, bar)
    if stop_first is None:
        return None
    if stop_first:
        stop_fill = stop_market_fill(side, stop, bar, slippage)
        if stop_fill is None:
            raise AssertionError("OCO stop leg chosen but not triggered")
        return True, stop_fill
    tp_fill = limit_fill(side, take_profit, bar)
    if tp_fill is None:
        raise AssertionError("OCO take-profit leg chosen but not triggered")
    return False, tp_fill


def trailing_fill(
    side: str, extreme: Decimal, trail_pct: Decimal, bar: OHLCV, slippage: Decimal
) -> TrailingOutcome:
    """Walk the bar path once, moving the peak (SELL) or trough (BUY) as price gets there.

    The stop is checked against the extreme as of each point, so a new high later in the bar
    never triggers on a low that came before it. A gap through the stop at the open fills at the
    open (worse of trigger and open), otherwise at the trigger level; both slipped; taker.
    """
    s = _side(side)
    if not 0 < trail_pct < 1:
        raise ValueError(f"trail_pct must be in (0, 1), got {trail_pct}")
    path = intrabar_path(bar)
    for i, point in enumerate(path):
        trigger = extreme * (_ONE - trail_pct) if s == "SELL" else extreme * (_ONE + trail_pct)
        hit = point <= trigger if s == "SELL" else point >= trigger
        if hit:
            price = point if i == 0 else trigger
            return TrailingOutcome(extreme, FillPrice(slipped(price, s, slippage), "taker"))
        extreme = max(extreme, point) if s == "SELL" else min(extreme, point)
    return TrailingOutcome(extreme, None)
