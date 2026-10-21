"""Paper executor: a Binance SPOT account simulated on closed bars.

The account is a per-asset ledger of free and locked balances. A BUY can only spend free quote,
a SELL can only sell free base, so there are no naked shorts and no balance goes negative.
Resting orders lock what they may spend (SELL locks base, BUY locks quote, an OCO locks once).
Fees are always charged in the quote asset (a documented deviation from Binance, where a BUY
fee is taken in base unless paid with BNB).

Market orders fill with slippage at the latest traded price set by ``set_mark`` since the last
closed bar, else at that bar's close. Resting orders are evaluated only by ``on_closed_bar``
with the rules in ``hydra.execution.fill_rules``; an order placed at a live mark sees only the
move from that mark to the close of the bar it was placed in. With a DB pool, every change
is persisted (``hydra.execution.paper_store``) before the call returns; ``restore`` rebuilds the
account after a restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, Protocol, cast, get_args

from hydra.core import metrics
from hydra.core.types import OHLCV, Direction, ExchangeId, OrderType, Position, Symbol
from hydra.execution import fill_rules, paper_store
from hydra.execution.fill_rules import FillPrice, Liquidity

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_STATE_FORMAT = 1
_RESTING_TYPES = (
    OrderType.LIMIT,
    OrderType.STOP_MARKET,
    OrderType.STOP_LIMIT,
    OrderType.TRAILING_STOP,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InsufficientBalanceError(ValueError):
    """The order needs more free balance than the account holds (spot: no naked shorts)."""


class OrderRejectedError(ValueError):
    """The order breaks a market rule (step size, minimum notional) or is malformed."""


class PersistenceUnavailableError(RuntimeError):
    """Fills are not yet persisted; new risk-increasing orders are refused until they are."""


class LedgerInvariantError(RuntimeError):
    """A balance, lock or lot broke a ledger invariant; the account must not keep trading."""


class PaperSessionHaltedError(RuntimeError):
    """The account is halted (broken invariant or a second writer); only ``flatten`` still runs."""


class PaperStateUnavailableError(RuntimeError):
    """The session's paper state cannot be restored although the DB shows it traded."""


class PaperStateCorruptError(PaperStateUnavailableError):
    """The stored paper state does not satisfy the ledger invariants."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


class SpotMarketRules(Protocol):
    """Exchange filters for one symbol; ``hydra.risk.market_rules.MarketRules`` satisfies it."""

    @property
    def amount_step(self) -> Decimal | None: ...

    @property
    def min_qty(self) -> Decimal | None: ...

    @property
    def min_notional(self) -> Decimal | None: ...


PaperFillStatus = Literal["FILLED", "CANCELLED"]
MarkSource = Literal["live", "stale_live", "last_close"]


@dataclass(frozen=True, slots=True)
class PaperFill:
    """One executed paper fill, for the session layer to publish as an ``OrderFillEvent``.

    ``gross_pnl`` and ``net_pnl`` are None on opening (BUY) fills; ``net_pnl`` is net of this
    fill's fee and the pro-rata entry fee. ``parent_order_id`` is the OCO id for an OCO leg.
    ``status="CANCELLED"`` reports a resting BUY dropped on a gap it could not pay for: nothing
    traded, ``price`` is where it would have filled, ``fee`` is 0 and ``fill_seq`` is 0.
    ``mark_source`` is set on market fills: ``live`` (a ``set_mark`` price newer than the last
    closed bar), ``stale_live`` (such a price, but the feed went stale after it) or
    ``last_close`` (the fallback); None on bar-priced and restored fills.
    """

    order_id: str
    parent_order_id: str | None
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    fee_currency: str
    liquidity: Liquidity
    gross_pnl: Decimal | None
    net_pnl: Decimal | None
    reason: str
    timestamp: datetime
    fill_seq: int
    status: PaperFillStatus = "FILLED"
    mark_source: MarkSource | None = None


@dataclass(frozen=True, slots=True)
class PaperAccountSnapshot:
    """Account totals in the quote asset; ``drawdown`` is a fraction of ``peak_equity``."""

    quote_asset: str
    quote_free: Decimal
    quote_locked: Decimal
    base_totals: dict[str, Decimal]
    marks: dict[str, Decimal]
    equity: Decimal
    initial_equity: Decimal
    realized_gross: Decimal
    fees_paid: Decimal
    realized_net: Decimal
    unrealized: Decimal
    peak_equity: Decimal
    drawdown: Decimal
    open_orders: int
    last_bar_ts: dict[str, datetime]
    persist_dirty: bool
    halted: str | None


@dataclass(slots=True)
class _Lock:
    asset: str
    amount: Decimal


@dataclass(slots=True)
class _Lot:
    base_qty: Decimal = _ZERO
    price_basis: Decimal = _ZERO
    entry_fees_open: Decimal = _ZERO
    realized_net: Decimal = _ZERO


@dataclass(slots=True)
class _PendingOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    lock_id: str
    seq: int
    reason: str
    created_after_ts: datetime | None
    trail_pct: Decimal | None = None
    extreme: Decimal | None = None
    oco_id: str | None = None
    placed_at: Decimal | None = None


@dataclass(slots=True)
class _Rules:
    step: Decimal | None
    min_qty: Decimal | None
    min_notional: Decimal | None


@dataclass(slots=True)
class _Ledger:
    free: dict[str, Decimal] = field(default_factory=dict)
    locked: dict[str, Decimal] = field(default_factory=dict)
    locks: dict[str, _Lock] = field(default_factory=dict)

    def get_free(self, asset: str) -> Decimal:
        return self.free.get(asset, _ZERO)

    def get_locked(self, asset: str) -> Decimal:
        return self.locked.get(asset, _ZERO)

    def total(self, asset: str) -> Decimal:
        return self.get_free(asset) + self.get_locked(asset)

    def credit(self, asset: str, amount: Decimal) -> None:
        self.free[asset] = self.get_free(asset) + amount

    def debit(self, asset: str, amount: Decimal) -> None:
        if self.get_free(asset) < amount:
            raise InsufficientBalanceError(
                f"Insufficient {asset} balance: need {amount}, free {self.get_free(asset)}"
            )
        self.free[asset] = self.get_free(asset) - amount

    def lock(self, lock_id: str, asset: str, amount: Decimal) -> None:
        self.debit(asset, amount)
        self.locked[asset] = self.get_locked(asset) + amount
        self.locks[lock_id] = _Lock(asset, amount)

    def release(self, lock_id: str) -> None:
        held = self.locks.pop(lock_id, None)
        if held is None:
            return
        self.locked[held.asset] = self.get_locked(held.asset) - held.amount
        self.credit(held.asset, held.amount)


def _canon_symbol(symbol: str) -> str:
    return symbol.split(":", 1)[0].replace("/", "").replace("-", "").upper()


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _opt_d(value: object) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _opt_s(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _since_placement(mark: Decimal, bar: OHLCV) -> OHLCV:
    """The part of ``bar`` known to follow an order placed at ``mark``: mark, then the close."""
    return OHLCV(
        open=mark,
        high=max(mark, bar.close),
        low=min(mark, bar.close),
        close=bar.close,
        volume=bar.volume,
        timestamp=bar.timestamp,
    )


def _ts(value: datetime) -> str:
    return value.isoformat()


def _parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# PaperTradingExecutor
# ---------------------------------------------------------------------------


class PaperTradingExecutor:
    """Simulated Binance SPOT account for one paper session.

    ``fee_pct`` sets both maker and taker fees unless they are given explicitly. ``step_size``
    and ``min_notional`` apply to every symbol; ``market_rules`` overrides them per symbol.
    """

    def __init__(
        self,
        exchange_id: ExchangeId = "binance",
        initial_balances: Mapping[str, Decimal] | None = None,
        slippage_pct: Decimal = Decimal("0.001"),
        fee_pct: Decimal | None = None,
        db_pool: Any = None,
        strategy_id: str = "",
        session_id: str = "",
        *,
        maker_fee: Decimal | None = None,
        taker_fee: Decimal | None = None,
        quote_asset: str = "USDT",
        step_size: Decimal | None = None,
        min_notional: Decimal | None = None,
        market_rules: Mapping[str, SpotMarketRules] | None = None,
    ) -> None:
        default_fee = Decimal("0.001") if fee_pct is None else fee_pct
        self._maker_fee = default_fee if maker_fee is None else maker_fee
        self._taker_fee = default_fee if taker_fee is None else taker_fee
        self._slippage_pct = slippage_pct
        for name, value in (
            ("maker_fee", self._maker_fee),
            ("taker_fee", self._taker_fee),
            ("slippage_pct", slippage_pct),
        ):
            if not _ZERO <= value < _ONE:
                raise ValueError(f"{name} must be in [0, 1), got {value}")
        if db_pool is not None and not session_id:
            raise ValueError("a paper executor with a DB pool needs a session_id")
        self._exchange_id: ExchangeId = exchange_id
        self._quote = quote_asset.upper()
        self._db_pool = db_pool
        self._strategy_id = strategy_id
        self._session_id = session_id
        self._step_size = step_size
        self._min_notional = min_notional
        self._market_rules: dict[str, SpotMarketRules] = {
            _canon_symbol(k): v for k, v in (market_rules or {}).items()
        }

        balances = dict(initial_balances or {self._quote: Decimal("10000")})
        unknown = sorted(a for a in balances if a.upper() != self._quote)
        if unknown:
            raise ValueError(f"paper accounts start with {self._quote} only, got {unknown}")
        start = _d(balances.get(self._quote, balances.get(self._quote.lower(), _ZERO)))
        if start < 0:
            raise ValueError(f"initial {self._quote} balance must be >= 0, got {start}")

        self._ledger = _Ledger(free={self._quote: start})
        self._lots: dict[str, _Lot] = {}
        self._pending: list[_PendingOrder] = []
        self._marks: dict[str, Decimal] = {}
        self._live_marks: dict[str, Decimal] = {}
        self._stale_marks: set[str] = set()
        self._last_bar_ts: dict[str, datetime] = {}
        self._names: dict[str, str] = {}
        self._fill_seq = 0
        self._order_seq = 0
        self._initial_equity = start
        self._peak_equity = start
        self._realized_gross = _ZERO
        self._fees_paid = _ZERO
        self._realized_net = _ZERO
        self._filled: list[PaperFill] = []
        self._outbox: list[PaperFill] = []
        self._dirty = False
        self._state_version = 0
        self._unconfirmed: set[str] = set()
        self._halted: str | None = None
        self._persist_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Symbols, rules, marks
    # ------------------------------------------------------------------

    def _symbol(self, symbol: str) -> str:
        canon = _canon_symbol(symbol)
        if not canon.endswith(self._quote) or canon == self._quote:
            raise OrderRejectedError(f"{symbol} is not a {self._quote}-quoted spot symbol")
        self._names.setdefault(canon, symbol)
        return canon

    def _base(self, canon: str) -> str:
        return canon[: -len(self._quote)]

    def _name(self, canon: str) -> str:
        return self._names.get(canon, canon)

    def _rules(self, canon: str) -> _Rules:
        r = self._market_rules.get(canon)
        if r is None:
            return _Rules(self._step_size, None, self._min_notional)
        return _Rules(r.amount_step, r.min_qty, r.min_notional)

    def _check_rules(self, canon: str, quantity: Decimal, price: Decimal) -> None:
        rules = self._rules(canon)
        if rules.step is not None and rules.step > 0 and quantity % rules.step != 0:
            raise OrderRejectedError(f"quantity {quantity} is not a multiple of step {rules.step}")
        if rules.min_qty is not None and quantity < rules.min_qty:
            raise OrderRejectedError(f"quantity {quantity} is below the minimum {rules.min_qty}")
        if rules.min_notional is not None and quantity * price < rules.min_notional:
            raise OrderRejectedError(
                f"notional {quantity * price} is below the minimum {rules.min_notional}"
            )

    def _fee_rate(self, liquidity: Liquidity) -> Decimal:
        return self._maker_fee if liquidity == "maker" else self._taker_fee

    def _bar_time(self, canon: str) -> datetime:
        return self._last_bar_ts.get(canon) or datetime.now(UTC)

    def set_mark(self, symbol: str, price: Decimal) -> None:
        """Latest traded price between closed bars; only market fills use it, until the next bar."""
        if price > 0:
            canon = self._symbol(symbol)
            self._live_marks[canon] = price
            self._stale_marks.discard(canon)

    def mark_feed_stale(self) -> None:
        """The feed went stale: the live marks held now are labelled until a fresh one arrives."""
        self._stale_marks = set(self._live_marks)

    def _market_mark(self, canon: str) -> tuple[Decimal | None, MarkSource]:
        live = self._live_marks.get(canon)
        if live is not None:
            return live, "stale_live" if canon in self._stale_marks else "live"
        return self._marks.get(canon), "last_close"

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        price: Decimal | None = None,
        stop_price: Decimal | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Market orders fill at once at the latest mark; other types rest and lock funds.

        OCO: ``price`` is the take-profit limit, ``stop_price`` the stop-loss trigger.
        """
        self._require_running()
        canon = self._symbol(symbol)
        s = side.upper()
        otype = order_type.upper()
        if s not in ("BUY", "SELL"):
            raise OrderRejectedError(f"side must be BUY or SELL, got {side!r}")
        quantity = _d(quantity)
        if quantity <= 0:
            raise OrderRejectedError(f"quantity must be > 0, got {quantity}")
        if s == "BUY":
            await self._require_persisted()

        if otype == OrderType.MARKET:
            mark, source = self._market_mark(canon)
            if mark is None or mark <= 0:
                raise ValueError(f"No market price available for {symbol}")
            fp = fill_rules.market_fill(s, mark, self._slippage_pct)
            self._check_rules(canon, quantity, fp.price)
            fill = self._fill_from_free(
                canon, s, OrderType.MARKET, quantity, fp, "market", str(uuid.uuid4()), source
            )
            await self._persist(snapshot_at=fill.timestamp)
            return {**self._fill_response(fill), "mark_source": source}

        if otype == OrderType.OCO:
            return await self._create_oco(canon, s, quantity, price, stop_price)
        if otype not in _RESTING_TYPES:
            raise OrderRejectedError(
                f"order type {order_type} is not supported by the paper executor"
            )
        order = self._new_resting(canon, s, otype, quantity, price, stop_price, params or {})
        self._pending.append(order)
        await self._persist(snapshot_at=None)
        return self._pending_response(order)

    def _new_resting(
        self,
        canon: str,
        side: str,
        otype: str,
        quantity: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
        params: dict[str, Any],
    ) -> _PendingOrder:
        price = _opt_d(price)
        stop_price = _opt_d(stop_price)
        trail_pct: Decimal | None = None
        extreme: Decimal | None = None
        if otype == OrderType.LIMIT:
            if price is None or price <= 0:
                raise OrderRejectedError("LIMIT orders need a positive price")
            ref = price
            reason = "limit"
        elif otype == OrderType.STOP_MARKET:
            stop_price = stop_price if stop_price is not None else price
            price = None
            if stop_price is None or stop_price <= 0:
                raise OrderRejectedError("STOP_MARKET orders need a positive stop_price")
            ref = stop_price
            reason = "stop_loss" if side == "SELL" else "stop_entry"
        elif otype == OrderType.STOP_LIMIT:
            if price is None or stop_price is None or price <= 0 or stop_price <= 0:
                raise OrderRejectedError("STOP_LIMIT orders need a positive price and stop_price")
            ref = price
            reason = "stop_loss" if side == "SELL" else "stop_entry"
        else:
            raw = params.get("trail_pct")
            if raw is None:
                raise ValueError("trail_pct is required for TRAILING_STOP orders")
            trail_pct = _d(raw)
            if not _ZERO < trail_pct < _ONE:
                raise OrderRejectedError(f"trail_pct must be in (0, 1), got {trail_pct}")
            mark, _ = self._market_mark(canon)
            if mark is None:
                raise ValueError(f"No market price available for {canon}")
            extreme = mark
            ref = mark * (_ONE - trail_pct) if side == "SELL" else mark * (_ONE + trail_pct)
            price = None
            stop_price = None
            reason = "trailing_stop"
        self._check_rules(canon, quantity, ref)

        order_id = str(uuid.uuid4())
        if side == "SELL":
            self._ledger.lock(order_id, self._base(canon), quantity)
        else:
            self._ledger.lock(order_id, self._quote, self._buy_lock(otype, quantity, ref))
        self._order_seq += 1
        return _PendingOrder(
            order_id=order_id,
            symbol=canon,
            side=side,
            order_type=otype,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            lock_id=order_id,
            seq=self._order_seq,
            reason=reason,
            created_after_ts=self._last_bar_ts.get(canon),
            trail_pct=trail_pct,
            extreme=extreme,
            placed_at=self._live_marks.get(canon),
        )

    def _buy_lock(self, otype: str, quantity: Decimal, ref: Decimal) -> Decimal:
        fee = max(self._maker_fee, self._taker_fee)
        if otype in (OrderType.STOP_MARKET, OrderType.TRAILING_STOP):
            return quantity * ref * (_ONE + self._slippage_pct) * (_ONE + fee)
        return quantity * ref * (_ONE + fee)

    async def _create_oco(
        self,
        canon: str,
        side: str,
        quantity: Decimal,
        price: Decimal | None,
        stop_price: Decimal | None,
    ) -> dict[str, Any]:
        if price is None or stop_price is None:
            raise ValueError(
                "OCO orders require both price (take-profit limit) "
                "and stop_price (stop-loss trigger)"
            )
        tp, sl = _d(price), _d(stop_price)
        if side == "SELL" and not sl < tp:
            raise OrderRejectedError(f"SELL OCO needs stop {sl} below take-profit {tp}")
        if side == "BUY" and not tp < sl:
            raise OrderRejectedError(f"BUY OCO needs take-profit {tp} below stop {sl}")
        self._check_rules(canon, quantity, min(tp, sl))
        oco_id = str(uuid.uuid4())
        if side == "SELL":
            self._ledger.lock(oco_id, self._base(canon), quantity)
        else:
            self._ledger.lock(
                oco_id, self._quote, self._buy_lock(OrderType.STOP_MARKET, quantity, sl)
            )
        created = self._last_bar_ts.get(canon)
        placed_at = self._live_marks.get(canon)
        legs: list[_PendingOrder] = []
        for otype, leg_price, leg_stop, reason in (
            (OrderType.LIMIT, tp, None, "take_profit"),
            (OrderType.STOP_MARKET, None, sl, "stop_loss"),
        ):
            self._order_seq += 1
            legs.append(
                _PendingOrder(
                    order_id=str(uuid.uuid4()),
                    symbol=canon,
                    side=side,
                    order_type=otype,
                    quantity=quantity,
                    price=leg_price,
                    stop_price=leg_stop,
                    lock_id=oco_id,
                    seq=self._order_seq,
                    reason=reason,
                    created_after_ts=created,
                    oco_id=oco_id,
                    placed_at=placed_at,
                )
            )
        self._pending.extend(legs)
        await self._persist(snapshot_at=None)
        return {
            "id": oco_id,
            "symbol": self._name(canon),
            "side": side,
            "type": OrderType.OCO.value,
            "amount": float(quantity),
            "price": float(tp),
            "stop_price": float(sl),
            "status": "PENDING",
            "filled": False,
            "oco_link": oco_id,
            "take_profit_order_id": legs[0].order_id,
            "stop_loss_order_id": legs[1].order_id,
        }

    async def cancel_order(self, order_id: str, symbol: str) -> dict[str, Any]:
        """Cancel a resting order (an OCO by its id or either leg) and release its lock."""
        target = next(
            (o for o in self._pending if order_id in (o.order_id, o.oco_id)),
            None,
        )
        if target is None:
            raise ValueError(f"Order {order_id} not found in pending orders")
        self._remove_order(target)
        await self._persist(snapshot_at=None)
        return {"id": order_id, "status": "CANCELLED"}

    def _remove_order(self, order: _PendingOrder) -> None:
        if order.oco_id is not None:
            self._pending = [o for o in self._pending if o.oco_id != order.oco_id]
        else:
            self._pending = [o for o in self._pending if o.order_id != order.order_id]
        self._ledger.release(order.lock_id)

    # ------------------------------------------------------------------
    # Closed bars
    # ------------------------------------------------------------------

    async def on_closed_bar(self, symbol: str, ohlcv: OHLCV) -> list[PaperFill]:
        """Evaluate this symbol's resting orders on one closed bar, then mark to its close.

        A bar at or before the last processed timestamp is ignored. Orders placed after the
        previous bar are eligible; one placed at a live mark sees this bar as the path from that
        mark to the close (its earlier range came before the order). At most one leg of an OCO
        fills. The result also carries
        ``status="CANCELLED"`` records for BUYs dropped on a gap. While halted, only SELLs run.
        """
        canon = self._symbol(symbol)
        last = self._last_bar_ts.get(canon)
        if last is not None and ohlcv.timestamp <= last:
            return []
        fills: list[PaperFill] = []
        seen_oco: set[str] = set()
        for order in sorted((o for o in self._pending if o.symbol == canon), key=lambda o: o.seq):
            if order not in self._pending or order.oco_id in seen_oco:
                continue
            if order.oco_id is not None:
                seen_oco.add(order.oco_id)
            if order.created_after_ts is not None and ohlcv.timestamp <= order.created_after_ts:
                continue
            bar = ohlcv
            if order.placed_at is not None:
                bar = _since_placement(order.placed_at, ohlcv)
                for leg in self._pending:
                    if leg is order or (order.oco_id is not None and leg.oco_id == order.oco_id):
                        leg.placed_at = None
            if self._halted is not None and order.side == "BUY":
                continue
            fill = self._evaluate(order, bar)
            if fill is not None:
                fills.append(fill)
        self._marks[canon] = ohlcv.close
        self._live_marks.pop(canon, None)
        self._stale_marks.discard(canon)
        self._last_bar_ts[canon] = ohlcv.timestamp
        self._update_peak()
        await self._persist(snapshot_at=ohlcv.timestamp)
        return fills

    def _evaluate(self, order: _PendingOrder, bar: OHLCV) -> PaperFill | None:
        slip = self._slippage_pct
        if order.oco_id is not None:
            legs = [o for o in self._pending if o.oco_id == order.oco_id]
            tp = next(o for o in legs if o.order_type == OrderType.LIMIT)
            sl = next(o for o in legs if o.order_type == OrderType.STOP_MARKET)
            if tp.price is None or sl.stop_price is None:
                raise LedgerInvariantError(f"OCO {order.oco_id} is missing a leg price")
            outcome = fill_rules.oco_fill(order.side, sl.stop_price, tp.price, bar, slip)
            if outcome is None:
                return None
            stop_won, fp = outcome
            return self._fill_resting(sl if stop_won else tp, fp, bar.timestamp)

        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise LedgerInvariantError(f"LIMIT {order.order_id} has no price")
            limit = fill_rules.limit_fill(order.side, order.price, bar)
            return None if limit is None else self._fill_resting(order, limit, bar.timestamp)
        if order.order_type == OrderType.STOP_MARKET:
            if order.stop_price is None:
                raise LedgerInvariantError(f"STOP_MARKET {order.order_id} has no stop")
            stop = fill_rules.stop_market_fill(order.side, order.stop_price, bar, slip)
            return None if stop is None else self._fill_resting(order, stop, bar.timestamp)
        if order.order_type == OrderType.STOP_LIMIT:
            if order.stop_price is None or order.price is None:
                raise LedgerInvariantError(f"STOP_LIMIT {order.order_id} has no prices")
            sl_outcome = fill_rules.stop_limit_fill(order.side, order.stop_price, order.price, bar)
            if sl_outcome is None:
                return None
            if sl_outcome.fill is not None:
                return self._fill_resting(order, sl_outcome.fill, bar.timestamp)
            order.order_type = OrderType.LIMIT
            order.stop_price = None
            return None
        if order.extreme is None or order.trail_pct is None:
            raise LedgerInvariantError(f"TRAILING_STOP {order.order_id} has no peak")
        trail = fill_rules.trailing_fill(order.side, order.extreme, order.trail_pct, bar, slip)
        order.extreme = trail.extreme
        return None if trail.fill is None else self._fill_resting(order, trail.fill, bar.timestamp)

    def _fill_resting(self, order: _PendingOrder, fp: FillPrice, ts: datetime) -> PaperFill:
        self._remove_order(order)
        if order.side == "BUY":
            need = order.quantity * fp.price * (_ONE + self._fee_rate(fp.liquidity))
            if self._ledger.get_free(self._quote) < need:
                logger.warning(
                    "Paper %s %s cancelled: gap fill at %s needs %s %s, free %s",
                    order.order_type,
                    order.order_id,
                    fp.price,
                    need,
                    self._quote,
                    self._ledger.get_free(self._quote),
                )
                return PaperFill(
                    order_id=order.order_id,
                    parent_order_id=order.oco_id,
                    symbol=self._name(order.symbol),
                    side=order.side,
                    order_type=order.order_type,
                    quantity=order.quantity,
                    price=fp.price,
                    fee=_ZERO,
                    fee_currency=self._quote,
                    liquidity=fp.liquidity,
                    gross_pnl=None,
                    net_pnl=None,
                    reason="insufficient_balance_on_gap",
                    timestamp=ts,
                    fill_seq=0,
                    status="CANCELLED",
                )
        parent = order.oco_id
        return self._apply_fill(
            order.symbol,
            order.side,
            order.order_type,
            order.quantity,
            fp,
            order.reason,
            order.order_id,
            parent,
            ts,
        )

    # ------------------------------------------------------------------
    # Fills and the ledger
    # ------------------------------------------------------------------

    def _fill_from_free(
        self,
        canon: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        fp: FillPrice,
        reason: str,
        order_id: str,
        mark_source: MarkSource,
    ) -> PaperFill:
        if side == "BUY":
            need = quantity * fp.price * (_ONE + self._fee_rate(fp.liquidity))
            if self._ledger.get_free(self._quote) < need:
                raise InsufficientBalanceError(
                    f"Insufficient {self._quote} balance for buy: need {need}, "
                    f"free {self._ledger.get_free(self._quote)}"
                )
        else:
            base = self._base(canon)
            if self._ledger.get_free(base) < quantity:
                raise InsufficientBalanceError(
                    f"Insufficient {base} balance for sell: need {quantity}, "
                    f"free {self._ledger.get_free(base)} (spot cannot sell short)"
                )
        return self._apply_fill(
            canon,
            side,
            order_type,
            quantity,
            fp,
            reason,
            order_id,
            None,
            self._bar_time(canon),
            mark_source,
        )

    def _apply_fill(
        self,
        canon: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        fp: FillPrice,
        reason: str,
        order_id: str,
        parent_order_id: str | None,
        ts: datetime,
        mark_source: MarkSource | None = None,
    ) -> PaperFill:
        base = self._base(canon)
        notional = quantity * fp.price
        fee = notional * self._fee_rate(fp.liquidity)
        lot = self._lots.setdefault(canon, _Lot())
        gross: Decimal | None = None
        net: Decimal | None = None
        if side == "BUY":
            self._ledger.debit(self._quote, notional + fee)
            self._ledger.credit(base, quantity)
            lot.base_qty += quantity
            lot.price_basis += notional
            lot.entry_fees_open += fee
        else:
            self._ledger.debit(base, quantity)
            self._ledger.credit(self._quote, notional - fee)
            if quantity == lot.base_qty:
                basis_out, entry_fee_out = lot.price_basis, lot.entry_fees_open
            else:
                basis_out = lot.price_basis * quantity / lot.base_qty
                entry_fee_out = lot.entry_fees_open * quantity / lot.base_qty
            lot.base_qty -= quantity
            lot.price_basis -= basis_out
            lot.entry_fees_open -= entry_fee_out
            gross = notional - basis_out
            net = gross - fee - entry_fee_out
            lot.realized_net += net
            self._realized_gross += gross
            self._realized_net += net
        self._fees_paid += fee
        self._fill_seq += 1
        fill = PaperFill(
            order_id=order_id,
            parent_order_id=parent_order_id,
            symbol=self._name(canon),
            side=side,
            order_type=str(order_type),
            quantity=quantity,
            price=fp.price,
            fee=fee,
            fee_currency=self._quote,
            liquidity=fp.liquidity,
            gross_pnl=gross,
            net_pnl=net,
            reason=reason,
            timestamp=ts,
            fill_seq=self._fill_seq,
            mark_source=mark_source,
        )
        self._filled.append(fill)
        if self._db_pool is not None:
            self._outbox.append(fill)
        try:
            self._check_invariants()
        except LedgerInvariantError as exc:
            if self._halted is not None:
                logger.warning(
                    "Paper session %s exit on a halted ledger: %s", self._session_id, exc
                )
            else:
                self._halt(f"ledger invariant broken: {exc}")
                raise
        self._update_peak()
        return fill

    def _check_invariants(self) -> None:
        ledger = self._ledger
        for asset in set(ledger.free) | set(ledger.locked):
            if ledger.get_free(asset) < 0 or ledger.get_locked(asset) < 0:
                raise LedgerInvariantError(f"negative {asset} balance")
            held = sum((lk.amount for lk in ledger.locks.values() if lk.asset == asset), _ZERO)
            if held != ledger.get_locked(asset):
                raise LedgerInvariantError(f"{asset} locked {ledger.get_locked(asset)} != {held}")
        for canon, lot in self._lots.items():
            if lot.base_qty < 0 or lot.price_basis < 0 or lot.entry_fees_open < 0:
                raise LedgerInvariantError(f"negative lot for {canon}")
            if lot.base_qty != ledger.total(self._base(canon)):
                raise LedgerInvariantError(
                    f"{canon} lot {lot.base_qty} != {self._base(canon)} balance "
                    f"{ledger.total(self._base(canon))}"
                )
        lot_assets = {self._base(c) for c in self._lots}
        for asset in set(ledger.free) | set(ledger.locked):
            if asset != self._quote and asset not in lot_assets and ledger.total(asset) != 0:
                raise LedgerInvariantError(f"{asset} balance without a lot")
        for order in self._pending:
            if order.lock_id not in ledger.locks:
                raise LedgerInvariantError(f"order {order.order_id} has no lock")

    # ------------------------------------------------------------------
    # Flatten
    # ------------------------------------------------------------------

    async def flatten(self, symbol: str | None = None, reason: str = "flatten") -> list[PaperFill]:
        """Cancel resting orders and market-sell all base at the latest mark; never risk-gated."""
        if symbol is not None:
            targets = {self._symbol(symbol)}
        else:
            targets = {o.symbol for o in self._pending} | set(self._lots)
        fills: list[PaperFill] = []
        for canon in sorted(targets):
            for order in [o for o in self._pending if o.symbol == canon]:
                if order in self._pending:
                    self._remove_order(order)
            qty = self._ledger.get_free(self._base(canon))
            if qty <= 0:
                continue
            mark, source = self._market_mark(canon)
            if mark is None or mark <= 0:
                raise ValueError(f"cannot flatten {canon}: no market price for {qty} held")
            fp = fill_rules.market_fill("SELL", mark, self._slippage_pct)
            fills.append(
                self._apply_fill(
                    canon,
                    "SELL",
                    OrderType.MARKET,
                    qty,
                    fp,
                    reason,
                    str(uuid.uuid4()),
                    None,
                    self._bar_time(canon),
                    source,
                )
            )
        await self._persist(snapshot_at=fills[-1].timestamp if fills else None)
        return fills

    # ------------------------------------------------------------------
    # Equity
    # ------------------------------------------------------------------

    def _mark_value(self, canon: str, lot: _Lot) -> Decimal:
        mark = self._marks.get(canon)
        if mark is None:
            return lot.price_basis
        return lot.base_qty * mark

    def _equity(self) -> Decimal:
        total = self._ledger.total(self._quote)
        for canon, lot in self._lots.items():
            if lot.base_qty > 0:
                total += self._mark_value(canon, lot)
        return total

    def _unrealized(self) -> Decimal:
        return sum(
            (self._mark_value(c, lot) - lot.price_basis for c, lot in self._lots.items()),
            _ZERO,
        )

    def _update_peak(self) -> None:
        self._peak_equity = max(self._peak_equity, self._equity())

    def _drawdown(self, equity: Decimal) -> Decimal:
        if self._peak_equity <= 0:
            return _ZERO
        return max(_ZERO, (self._peak_equity - equity) / self._peak_equity)

    async def get_equity(self) -> Decimal:
        """Quote total plus every base total at its last mark (ExecutorState protocol)."""
        return self._equity()

    def get_account_snapshot(self) -> PaperAccountSnapshot:
        equity = self._equity()
        return PaperAccountSnapshot(
            quote_asset=self._quote,
            quote_free=self._ledger.get_free(self._quote),
            quote_locked=self._ledger.get_locked(self._quote),
            base_totals={
                self._base(c): self._ledger.total(self._base(c)) for c in sorted(self._lots)
            },
            marks=dict(self._marks),
            equity=equity,
            initial_equity=self._initial_equity,
            realized_gross=self._realized_gross,
            fees_paid=self._fees_paid,
            realized_net=self._realized_net,
            unrealized=self._unrealized(),
            peak_equity=self._peak_equity,
            drawdown=self._drawdown(equity),
            open_orders=len(self._pending),
            last_bar_ts=dict(self._last_bar_ts),
            persist_dirty=self._dirty,
            halted=self._halted,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        if self._session_id:
            metrics.set_paper_persist_dirty(self._session_id, dirty)

    def _halt(self, reason: str) -> None:
        if self._halted is None:
            logger.error("Paper session %s halted: %s", self._session_id, reason)
            self._halted = reason

    def _require_running(self) -> None:
        if self._halted is not None:
            raise PaperSessionHaltedError(
                f"paper session {self._session_id} is halted ({self._halted}); "
                "only flatten() still runs"
            )

    async def _require_persisted(self) -> None:
        if not self._dirty:
            return
        await self._persist(snapshot_at=None)
        if self._dirty:
            raise PersistenceUnavailableError(
                f"paper session {self._session_id} has {len(self._outbox)} unpersisted fills; "
                "new entries are refused until the database accepts them"
            )

    async def checkpoint(self) -> None:
        """Persist the current state (e.g. right after session start, before any fill)."""
        await self._persist(snapshot_at=None)

    async def _persist(self, *, snapshot_at: datetime | None) -> None:
        if self._db_pool is None:
            return
        async with self._persist_lock:
            await self._write(snapshot_at)

    async def _write(self, snapshot_at: datetime | None) -> None:
        if self._halted is not None:
            return
        version = self._state_version + 1
        account = paper_store.AccountRow(
            session_id=self._session_id,
            strategy_id=self._strategy_id,
            exchange_id=self._exchange_id,
            quote_asset=self._quote,
            state=self.to_state(),
            state_version=version,
        )
        trades = [
            paper_store.TradeRow(
                symbol=f.symbol,
                side=f.side,
                price=f.price,
                quantity=f.quantity,
                fee=f.fee,
                gross_pnl=f.gross_pnl if f.gross_pnl is not None else _ZERO,
                net_pnl=f.net_pnl,
                order_id=f.parent_order_id or f.order_id,
                order_type=f.order_type,
                fill_seq=f.fill_seq,
                liquidity=f.liquidity,
                reason=f.reason,
                timestamp=f.timestamp,
            )
            for f in self._outbox
        ]
        positions = []
        flat = []
        for canon, lot in sorted(self._lots.items()):
            if lot.base_qty > 0:
                positions.append(
                    paper_store.PositionRow(
                        symbol=self._name(canon),
                        quantity=lot.base_qty,
                        avg_entry_price=lot.price_basis / lot.base_qty,
                        unrealized_pnl=self._mark_value(canon, lot) - lot.price_basis,
                        realized_pnl=lot.realized_net,
                    )
                )
            else:
                flat.append(self._name(canon))
        snapshot = None
        if snapshot_at is not None:
            equity = self._equity()
            snapshot = paper_store.SnapshotRow(
                timestamp=snapshot_at,
                total_value=equity,
                unrealized_pnl=self._unrealized(),
                realized_pnl=self._realized_net,
                drawdown_pct=self._drawdown(equity),
                peak_value=self._peak_equity,
            )
        try:
            async with self._db_pool.acquire() as conn:
                await paper_store.save(conn, account, trades, positions, flat, snapshot)
        except paper_store.StaleStateError:
            if await self._adopt_landed_write():
                await self._write(snapshot_at)
            return
        except Exception:
            logger.exception(
                "Paper state for session %s not persisted; %d fills held in memory",
                self._session_id,
                len(self._outbox),
            )
            self._unconfirmed.add(_state_digest(account.state))
            self._set_dirty(True)
            return
        self._unconfirmed.clear()
        self._state_version = version
        written = {t.fill_seq for t in trades}
        self._outbox = [f for f in self._outbox if f.fill_seq not in written]
        if self._dirty and not self._outbox:
            self._set_dirty(False)

    async def _adopt_landed_write(self) -> bool:
        """After a refused write: adopt the stored version if it is one of our failed writes.

        A commit whose acknowledgement was lost leaves that state in the DB at our next version;
        any other stored state means a second executor writes this session, so we halt.
        """
        try:
            async with self._db_pool.acquire() as conn:
                stored = await paper_store.load(conn, self._session_id)
        except Exception:
            logger.exception("Paper state for session %s could not be re-read", self._session_id)
            self._set_dirty(True)
            return False
        if (
            stored is not None
            and stored.state_version == self._state_version + 1
            and _state_digest(stored.state) in self._unconfirmed
        ):
            self._state_version = stored.state_version
            self._unconfirmed.clear()
            return True
        stored_version = None if stored is None else stored.state_version
        self._set_dirty(True)
        self._halt(
            f"paper_accounts row is at version {stored_version}, written by another executor "
            f"(ours is {self._state_version})"
        )
        return False

    def to_state(self) -> dict[str, Any]:
        """The full account as a JSON-ready dict (Decimals as strings)."""
        return {
            "format": _STATE_FORMAT,
            "quote_asset": self._quote,
            "maker_fee": str(self._maker_fee),
            "taker_fee": str(self._taker_fee),
            "slippage_pct": str(self._slippage_pct),
            "step_size": _opt_s(self._step_size),
            "min_notional": _opt_s(self._min_notional),
            "free": {a: str(v) for a, v in sorted(self._ledger.free.items())},
            "locked": {a: str(v) for a, v in sorted(self._ledger.locked.items())},
            "locks": {
                k: {"asset": v.asset, "amount": str(v.amount)}
                for k, v in sorted(self._ledger.locks.items())
            },
            "lots": {
                c: {
                    "base_qty": str(lot.base_qty),
                    "price_basis": str(lot.price_basis),
                    "entry_fees_open": str(lot.entry_fees_open),
                    "realized_net": str(lot.realized_net),
                }
                for c, lot in sorted(self._lots.items())
            },
            "pending": [
                {
                    "order_id": o.order_id,
                    "symbol": o.symbol,
                    "side": o.side,
                    "order_type": o.order_type,
                    "quantity": str(o.quantity),
                    "price": _opt_s(o.price),
                    "stop_price": _opt_s(o.stop_price),
                    "lock_id": o.lock_id,
                    "seq": o.seq,
                    "reason": o.reason,
                    "created_after_ts": (
                        None if o.created_after_ts is None else _ts(o.created_after_ts)
                    ),
                    "trail_pct": _opt_s(o.trail_pct),
                    "extreme": _opt_s(o.extreme),
                    "oco_id": o.oco_id,
                    "placed_at": _opt_s(o.placed_at),
                }
                for o in self._pending
            ],
            "marks": {c: str(v) for c, v in sorted(self._marks.items())},
            "last_bar_ts": {c: _ts(v) for c, v in sorted(self._last_bar_ts.items())},
            "names": dict(sorted(self._names.items())),
            "fill_seq": self._fill_seq,
            "order_seq": self._order_seq,
            "initial_equity": str(self._initial_equity),
            "peak_equity": str(self._peak_equity),
            "realized_gross": str(self._realized_gross),
            "fees_paid": str(self._fees_paid),
            "realized_net": str(self._realized_net),
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        *,
        exchange_id: ExchangeId = "binance",
        db_pool: Any = None,
        strategy_id: str = "",
        session_id: str = "",
        market_rules: Mapping[str, SpotMarketRules] | None = None,
    ) -> PaperTradingExecutor:
        """Rebuild an executor from ``to_state``; raises PaperStateCorruptError if invalid."""
        try:
            if state.get("format") != _STATE_FORMAT:
                raise ValueError(f"unknown paper state format {state.get('format')!r}")
            ex = cls(
                exchange_id=exchange_id,
                initial_balances={str(state["quote_asset"]): _ZERO},
                slippage_pct=_d(state["slippage_pct"]),
                db_pool=db_pool,
                strategy_id=strategy_id,
                session_id=session_id,
                maker_fee=_d(state["maker_fee"]),
                taker_fee=_d(state["taker_fee"]),
                quote_asset=str(state["quote_asset"]),
                step_size=_opt_d(state["step_size"]),
                min_notional=_opt_d(state["min_notional"]),
                market_rules=market_rules,
            )
            ex._ledger = _Ledger(
                free={a: _d(v) for a, v in state["free"].items()},
                locked={a: _d(v) for a, v in state["locked"].items()},
                locks={
                    k: _Lock(str(v["asset"]), _d(v["amount"])) for k, v in state["locks"].items()
                },
            )
            ex._lots = {
                c: _Lot(
                    base_qty=_d(v["base_qty"]),
                    price_basis=_d(v["price_basis"]),
                    entry_fees_open=_d(v["entry_fees_open"]),
                    realized_net=_d(v["realized_net"]),
                )
                for c, v in state["lots"].items()
            }
            ex._pending = [
                _PendingOrder(
                    order_id=str(o["order_id"]),
                    symbol=str(o["symbol"]),
                    side=str(o["side"]),
                    order_type=str(o["order_type"]),
                    quantity=_d(o["quantity"]),
                    price=_opt_d(o["price"]),
                    stop_price=_opt_d(o["stop_price"]),
                    lock_id=str(o["lock_id"]),
                    seq=int(o["seq"]),
                    reason=str(o["reason"]),
                    created_after_ts=(
                        None if o["created_after_ts"] is None else _parse_ts(o["created_after_ts"])
                    ),
                    trail_pct=_opt_d(o["trail_pct"]),
                    extreme=_opt_d(o["extreme"]),
                    oco_id=o["oco_id"],
                    placed_at=_opt_d(o.get("placed_at")),
                )
                for o in state["pending"]
            ]
            ex._marks = {c: _d(v) for c, v in state["marks"].items()}
            ex._last_bar_ts = {c: _parse_ts(v) for c, v in state["last_bar_ts"].items()}
            ex._names = {str(k): str(v) for k, v in state["names"].items()}
            ex._fill_seq = int(state["fill_seq"])
            ex._order_seq = int(state["order_seq"])
            ex._initial_equity = _d(state["initial_equity"])
            ex._peak_equity = _d(state["peak_equity"])
            ex._realized_gross = _d(state["realized_gross"])
            ex._fees_paid = _d(state["fees_paid"])
            ex._realized_net = _d(state["realized_net"])
            ex._check_invariants()
        except (KeyError, TypeError, ValueError, ArithmeticError, LedgerInvariantError) as exc:
            raise PaperStateCorruptError(f"paper state for session {session_id!r}: {exc}") from exc
        return ex

    @classmethod
    async def restore(
        cls,
        db_pool: Any,
        session_id: str,
        *,
        market_rules: Mapping[str, SpotMarketRules] | None = None,
    ) -> PaperTradingExecutor | None:
        """Rebuild the session's account from ``paper_accounts``.

        Returns None when the session has neither saved state nor open positions (start fresh).
        Raises PaperStateUnavailableError when positions exist without saved state or the DB cannot
        be read, and PaperStateCorruptError when the saved state fails the ledger invariants.
        """
        try:
            async with db_pool.acquire() as conn:
                stored = await paper_store.load(conn, session_id)
                orphaned = stored is None and await paper_store.has_open_positions(conn, session_id)
                trades = [] if stored is None else await paper_store.load_trades(conn, session_id)
        except Exception as exc:
            raise PaperStateUnavailableError(
                f"paper state for session {session_id!r} could not be read: {exc}"
            ) from exc
        if stored is None:
            if orphaned:
                raise PaperStateUnavailableError(
                    f"paper state unavailable (pre-P1 session) for {session_id!r}: "
                    "open positions exist without saved paper state"
                )
            return None
        ex = cls.from_state(
            stored.state,
            exchange_id=_exchange_id(stored.exchange_id),
            db_pool=db_pool,
            strategy_id=stored.strategy_id,
            session_id=stored.session_id,
            market_rules=market_rules,
        )
        ex._state_version = stored.state_version
        ex._filled = [ex._restored_fill(t) for t in trades]
        return ex

    def _restored_fill(self, t: paper_store.TradeRow) -> PaperFill:
        if t.liquidity not in ("maker", "taker"):
            raise PaperStateCorruptError(f"trade {t.fill_seq} has liquidity {t.liquidity!r}")
        return PaperFill(
            order_id=t.order_id,
            parent_order_id=None,
            symbol=t.symbol,
            side=t.side,
            order_type=t.order_type,
            quantity=t.quantity,
            price=t.price,
            fee=t.fee,
            fee_currency=self._quote,
            liquidity=cast(Liquidity, t.liquidity),
            gross_pnl=None if t.side == "BUY" else t.gross_pnl,
            net_pnl=t.net_pnl,
            reason=t.reason,
            timestamp=t.timestamp,
            fill_seq=t.fill_seq,
        )

    # ------------------------------------------------------------------
    # Responses and queries
    # ------------------------------------------------------------------

    @staticmethod
    def _fill_response(fill: PaperFill) -> dict[str, Any]:
        return {
            "id": fill.order_id,
            "symbol": fill.symbol,
            "side": fill.side,
            "type": fill.order_type,
            "amount": float(fill.quantity),
            "quantity": float(fill.quantity),
            "price": float(fill.price),
            "cost": float(fill.quantity * fill.price),
            "fee": {"cost": float(fill.fee), "currency": fill.fee_currency},
            "pnl": float(fill.gross_pnl or _ZERO),
            "net_pnl": None if fill.net_pnl is None else float(fill.net_pnl),
            "liquidity": fill.liquidity,
            "reason": fill.reason,
            "fill_seq": fill.fill_seq,
            "timestamp": fill.timestamp.isoformat(),
            "status": "FILLED",
            "filled": True,
        }

    def _pending_response(self, order: _PendingOrder) -> dict[str, Any]:
        return {
            "id": order.order_id,
            "symbol": self._name(order.symbol),
            "side": order.side,
            "type": order.order_type,
            "amount": float(order.quantity),
            "price": None if order.price is None else float(order.price),
            "stop_price": None if order.stop_price is None else float(order.stop_price),
            "status": "PENDING",
            "filled": False,
        }

    async def get_balance(self) -> dict[str, Decimal]:
        """Quote total, free plus locked (ExecutorState protocol).

        Callers still add this to position value, which then equals ``get_equity()``; they move to
        ``get_equity()`` / ``get_account_snapshot()`` in P1-C / P1-E.
        """
        return {self._quote: self._ledger.total(self._quote)}

    async def fetch_balance(self) -> dict[str, Decimal]:
        """Free balances of the quote and every base asset held (locked excluded, like ccxt)."""
        out = {self._quote: self._ledger.get_free(self._quote)}
        for asset, amount in sorted(self._ledger.free.items()):
            if asset != self._quote and amount > 0:
                out[asset] = amount
        return out

    def _open_lots(self, symbol: str | None) -> list[tuple[str, _Lot]]:
        wanted = None if symbol is None else _canon_symbol(symbol)
        return [
            (c, lot)
            for c, lot in sorted(self._lots.items())
            if lot.base_qty > 0 and (wanted is None or c == wanted)
        ]

    async def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open spot holdings in ccxt position shape (always long)."""
        return [
            {
                "symbol": self._name(c),
                "side": "long",
                "contracts": float(lot.base_qty),
                "entryPrice": float(lot.price_basis / lot.base_qty),
                "unrealizedPnl": float(self._mark_value(c, lot) - lot.price_basis),
                "realizedPnl": float(lot.realized_net),
            }
            for c, lot in self._open_lots(symbol)
        ]

    async def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Open holdings as Position objects (ExecutorState protocol); realized is net."""
        return [
            Position(
                symbol=Symbol(self._name(c)),
                direction=Direction.LONG,
                quantity=lot.base_qty,
                avg_entry_price=lot.price_basis / lot.base_qty,
                unrealized_pnl=self._mark_value(c, lot) - lot.price_basis,
                realized_pnl=lot.realized_net,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
            )
            for c, lot in self._open_lots(symbol)
        ]

    async def get_last_price(self, symbol: str) -> Decimal:
        """The last mark for a symbol (ExecutorState protocol)."""
        price = self._marks.get(_canon_symbol(symbol), _ZERO)
        if price <= 0:
            raise ValueError(f"No market price available for {symbol}")
        return price

    async def get_filled_orders(self) -> list[dict[str, Any]]:
        """Every fill so far, as response dicts."""
        return [self._fill_response(f) for f in self._filled]

    def get_fills(self) -> list[PaperFill]:
        return list(self._filled)

    async def fetch_open_orders(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Resting orders, including each stop's current trigger, reason and OCO id."""
        wanted = None if symbol is None else _canon_symbol(symbol)
        return [
            {
                **self._pending_response(o),
                "stop_price": _stop_trigger(o),
                "reason": o.reason,
                "oco_id": o.oco_id,
                "trail_pct": None if o.trail_pct is None else float(o.trail_pct),
            }
            for o in sorted(self._pending, key=lambda o: o.seq)
            if wanted is None or o.symbol == wanted
        ]


def _stop_trigger(o: _PendingOrder) -> float | None:
    """The stop's trigger; a trailing stop's moves with the peak (SELL) or trough (BUY)."""
    if o.extreme is not None and o.trail_pct is not None:
        return float(o.extreme * (_ONE - o.trail_pct if o.side == "SELL" else _ONE + o.trail_pct))
    return None if o.stop_price is None else float(o.stop_price)


def _state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(paper_store.dumps_state(state).encode()).hexdigest()


def _exchange_id(value: str) -> ExchangeId:
    if value not in get_args(ExchangeId):
        raise PaperStateCorruptError(f"unknown exchange_id {value!r} in paper state")
    return cast(ExchangeId, value)
