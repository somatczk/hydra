"""Perp paper executor: a Binance USD⌂-M isolated-margin account simulated on closed bars.

One margin wallet per session (the allocated capital) backs one isolated position per
symbol, long or short, at the strategy's leverage (1-3x, isolated only). An entry moves
``notional / leverage`` of margin into the position; the liquidation price is where the
position's equity (margin + unrealized PnL, funding included) falls to its maintenance
margin. Funding accrues only from settled rows in ``ts.funding_rates`` (the P4-B writer):
a window with no rows accrues nothing and is counted as missing, never fabricated. Fees
are charged in USDT; market orders fill at the latest mark with slippage, resting orders
evaluate on closed bars through ``hydra.execution.fill_rules`` — the same discipline the
spot executor follows, so paper sessions and backtests share this module (P2-A parity).

The per-bar sequence is fixed: resting orders, then liquidation, then funding, then the
mark. Funding values the position at the mark carried into the bar (no intrabar lookahead);
a position closed by its stop cannot be liquidated in the same bar, and a liquidated
position pays no funding for its final bar. Funding accruals are recorded as ``FUNDING``
fills on the account (visible in ``get_fills``) but are not order fills — they never
publish ``OrderFillEvent``s. Every order is capped to ``max_participation`` (10 % by
default) of the volume of the bar it fills on: an opening market order fills the fillable
slice and cancels the uncapped opening; a resting order fills its slice and the remainder
keeps working, same order id. A market order that would open exposure on a mark the feed
went stale over is refused; reduce-only exits and ``flatten`` keep filling at the stale
mark, labelled. A liquidation force-closes the position at its liquidation
price (the maintenance margin is the liquidation fee, the wallet gets nothing back), is
recorded on the account, and halts the session — the halt, the fill and the closed
position persist before anything else can touch the account, so a liquidated session
restores halted and never resumes; trading stops for it, only ``flatten`` still runs. A
broken-ledger halt (invariant failure, a second writer) is the opposite: the state is
never written again. Isolated margin caps every funding debit at the position's equity —
a payment that would cross the maintenance boundary liquidates the position honestly, at
the carried price, losing exactly its margin.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Literal, cast

from hydra.core import metrics
from hydra.core.types import OHLCV, Direction, ExchangeId, OrderType, Position, Symbol
from hydra.execution import fill_rules, paper_store
from hydra.execution.errors import UnsupportedVenueError
from hydra.execution.fill_rules import DEFAULT_MAX_PARTICIPATION, FillPrice, Liquidity
from hydra.execution.paper_trading import (
    InsufficientBalanceError,
    LedgerInvariantError,
    OrderRejectedError,
    PaperFill,
    PaperSessionHaltedError,
    PaperStateCorruptError,
    PaperStateUnavailableError,
    PaperTradingExecutor,
    PersistenceUnavailableError,
    SpotMarketRules,
    _canon_symbol,
    _d,
    _opt_d,
    _opt_s,
    _parse_ts,
    _since_placement,
    _ts,
    read_stored_account,
)

logger = logging.getLogger(__name__)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_MINUS_ONE = Decimal("-1")
_STATE_FORMAT = 3
PERP_EXCHANGE_ID: ExchangeId = "binanceusdm"
MAINTENANCE_MARGIN_RATE = Decimal("0.005")
"""The lowest USD⌂-M maintenance-margin tier (small notional brackets); a documented
approximation of Binance's tiered schedule, not a per-symbol table."""
SETTLEMENT_HOURS = frozenset({0, 1, 3, 8, 9, 11, 16, 17, 19})
"""Every hour Binance settles funding on some symbol (00/08/16, 01/09/17, 03/11/19 UTC).

Used only to *disclose* a settlement boundary that passed with no stored rows — never
to assume a schedule or invent a rate."""

FundingRow = tuple[datetime, Decimal]
FundingReader = Callable[[str, datetime, datetime], Awaitable[Sequence[FundingRow]]]
"""Rows (settled_at, rate) for one symbol in ``(start, end]``; the executor is offline."""

_RESTING_TYPES = (
    OrderType.LIMIT,
    OrderType.STOP_MARKET,
    OrderType.STOP_LIMIT,
    OrderType.TAKE_PROFIT_MARKET,
    OrderType.TRAILING_STOP,
)

_SELECT_FUNDING_SQL = """
    SELECT timestamp, rate FROM ts.funding_rates
    WHERE exchange = $1 AND symbol = $2 AND timestamp > $3 AND timestamp <= $4
    ORDER BY timestamp
"""


def pg_funding_reader(pool: Any, exchange_id: ExchangeId = PERP_EXCHANGE_ID) -> FundingReader:
    """Read settled funding rows for ``(start, end]`` from ``ts.funding_rates``."""

    async def reader(symbol: str, start: datetime, end: datetime) -> list[FundingRow]:
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_FUNDING_SQL, exchange_id, symbol, start, end)
        return [(r["timestamp"], Decimal(r["rate"])) for r in rows]

    return reader


def default_funding_reader(db_pool: Any, exchange_id: ExchangeId) -> FundingReader | None:
    """The session path's default: the PG reader for the perp venue, None without a DB."""
    if db_pool is None or exchange_id != PERP_EXCHANGE_ID:
        return None
    return pg_funding_reader(db_pool, exchange_id)


def static_funding_reader(rows: Mapping[str, Sequence[FundingRow]]) -> FundingReader:
    """A reader over preloaded rows (backtests, tests): only what was loaded accrues."""

    async def reader(symbol: str, start: datetime, end: datetime) -> list[FundingRow]:
        return [
            row
            for row in rows.get(symbol, rows.get(_canon_symbol(symbol), ()))
            if start < row[0] <= end
        ]

    return reader


def _resolve_fee(explicit: Decimal | None, fee_pct: Decimal | None, default: Decimal) -> Decimal:
    """An explicit per-side fee, else ``fee_pct``, else the venue default."""
    if explicit is not None:
        return explicit
    return fee_pct if fee_pct is not None else default


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PerpAccountSnapshot:
    """Perp account totals in the quote asset; ``drawdown`` is a fraction of ``peak_equity``.

    ``base_totals`` carries each symbol's position size (long or short, absolute), so
    ``holdings``-style reads work like the spot snapshot's. ``funding_applied`` counts
    settled rows accrued; ``funding_missing`` counts settlement windows an open position
    crossed with no stored rows (the honest skip); ``liquidations`` counts forced closes.
    """

    quote_asset: str
    wallet_available: Decimal
    margin_used: Decimal
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
    leverage: int
    funding_applied: int
    funding_missing: int
    liquidations: int


@dataclass(slots=True)
class _PerpPosition:
    qty: Decimal = _ZERO  # signed: positive long, negative short
    entry_price: Decimal = _ZERO
    margin: Decimal = _ZERO
    entry_fees_open: Decimal = _ZERO
    realized_net: Decimal = _ZERO
    funding_paid: Decimal = _ZERO  # positive = paid (margin reduced)


@dataclass(slots=True)
class _PendingOrder:
    order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    price: Decimal | None
    stop_price: Decimal | None
    seq: int
    reason: str
    created_after_ts: datetime | None
    reduce_only: bool = False
    trail_pct: Decimal | None = None
    extreme: Decimal | None = None
    oco_id: str | None = None
    placed_at: Decimal | None = None


@dataclass(slots=True)
class _Rules:
    step: Decimal | None
    min_qty: Decimal | None
    min_notional: Decimal | None


def _take_profit_fill(side: str, level: Decimal, bar: OHLCV, slippage: Decimal) -> FillPrice | None:
    """A TAKE_PROFIT_MARKET leg: market fill once price crosses *level*; taker."""
    if side == "SELL":
        if bar.high < level:
            return None
        return FillPrice(fill_rules.slipped(max(level, bar.open), side, slippage), "taker")
    if bar.low > level:
        return None
    return FillPrice(fill_rules.slipped(min(level, bar.open), side, slippage), "taker")


def _settlement_boundary_in(last: datetime | None, bar_ts: datetime) -> bool:
    """Whether an on-the-hour funding settlement instant lies in ``(last, bar_ts]``."""
    if last is None:
        return False
    probe = last.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    while probe <= bar_ts:
        if probe.hour in SETTLEMENT_HOURS:
            return True
        probe += timedelta(hours=1)
    return False


# ---------------------------------------------------------------------------
# PerpPaperTradingExecutor
# ---------------------------------------------------------------------------


class PerpPaperTradingExecutor:
    """Simulated Binance USD⌂-M isolated-margin account for one paper perp session.

    ``fee_pct`` sets both maker and taker fees unless given explicitly. ``leverage``
    (1-3) sets the margin per position and therefore its liquidation distance. With a
    DB pool every change persists (``hydra.execution.paper_store``) before the call
    returns; ``restore`` rebuilds the account after a restart.
    """

    def __init__(
        self,
        exchange_id: ExchangeId = PERP_EXCHANGE_ID,
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
        leverage: int = 1,
        maintenance_margin_rate: Decimal = MAINTENANCE_MARGIN_RATE,
        funding_reader: FundingReader | None = None,
        max_participation: Decimal = DEFAULT_MAX_PARTICIPATION,
    ) -> None:
        if exchange_id != PERP_EXCHANGE_ID:
            raise UnsupportedVenueError(
                f"the perp paper executor trades {PERP_EXCHANGE_ID} only, got {exchange_id}"
            )
        if not 1 <= leverage <= 3:
            raise ValueError(f"leverage must be 1-3, got {leverage}")
        if not _ZERO < maintenance_margin_rate < _ONE:
            raise ValueError(
                f"maintenance_margin_rate must be in (0, 1), got {maintenance_margin_rate}"
            )
        if not _ZERO <= max_participation <= _ONE:
            raise ValueError(f"max_participation must be in [0, 1], got {max_participation}")
        self._max_participation = max_participation
        # the USD⌂-M taker/maker defaults match CommissionConfig's futures rates, so a
        # session and a backtest on the same bars price fees identically
        self._maker_fee = _resolve_fee(maker_fee, fee_pct, Decimal("0.0002"))
        self._taker_fee = _resolve_fee(taker_fee, fee_pct, Decimal("0.0004"))
        self._slippage_pct = slippage_pct
        for name, value in (
            ("maker_fee", self._maker_fee),
            ("taker_fee", self._taker_fee),
            ("slippage_pct", slippage_pct),
        ):
            if not _ZERO <= value < _ONE:
                raise ValueError(f"{name} must be in [0, 1), got {value}")
        if db_pool is not None and not session_id:
            raise ValueError("a perp paper executor with a DB pool needs a session_id")
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
        self._leverage = leverage
        self._mmr = maintenance_margin_rate
        self._funding_reader = funding_reader

        balances = dict(initial_balances or {self._quote: Decimal("10000")})
        unknown = sorted(a for a in balances if a.upper() != self._quote)
        if unknown:
            raise ValueError(f"perp accounts start with {self._quote} only, got {unknown}")
        start = _d(balances.get(self._quote, balances.get(self._quote.lower(), _ZERO)))
        if start < 0:
            raise ValueError(f"initial {self._quote} balance must be >= 0, got {start}")

        self._wallet = start
        self._positions: dict[str, _PerpPosition] = {}
        self._pending: list[_PendingOrder] = []
        self._marks: dict[str, Decimal] = {}
        self._live_marks: dict[str, Decimal] = {}
        self._stale_marks: set[str] = set()
        self._last_bar_ts: dict[str, datetime] = {}
        self._bar_volume: dict[str, Decimal] = {}
        self._names: dict[str, str] = {}
        self._fill_seq = 0
        self._order_seq = 0
        self._initial_equity = start
        self._peak_equity = start
        self._realized_gross = _ZERO
        self._fees_paid = _ZERO
        self._realized_net = _ZERO
        self._funding_applied = 0
        self._funding_missing = 0
        self._liquidations = 0
        self._filled: list[PaperFill] = []
        self._outbox: list[PaperFill] = []
        self._dirty = False
        self._state_version = 0
        self._unconfirmed: set[str] = set()
        self._halted: str | None = None
        self._halt_broken = False
        self._persist_lock = asyncio.Lock()
        self._source = "paper"

    # ------------------------------------------------------------------
    # Symbols, rules, marks
    # ------------------------------------------------------------------

    def _symbol(self, symbol: str) -> str:
        canon = _canon_symbol(symbol)
        if not canon.endswith(self._quote) or canon == self._quote:
            raise OrderRejectedError(f"{symbol} is not a {self._quote}-quoted perp symbol")
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

    def _market_mark(self, canon: str) -> tuple[Decimal | None, str]:
        live = self._live_marks.get(canon)
        if live is not None:
            return live, "stale_live" if canon in self._stale_marks else "live"
        return self._marks.get(canon), "last_close"

    # ------------------------------------------------------------------
    # Margin math
    # ------------------------------------------------------------------

    @property
    def leverage(self) -> int:
        """The session's strategy leverage (recovery re-registers the session with it)."""
        return self._leverage

    def _margin_needed(self, notional: Decimal) -> Decimal:
        return notional / self._leverage

    def _liquidation_price(self, pos: _PerpPosition) -> Decimal | None:
        """Where the position's equity falls to its maintenance margin; None when unreachable."""
        qty = abs(pos.qty)
        if qty == 0:
            return None
        if pos.qty > 0:
            price = (qty * pos.entry_price - pos.margin) / (qty * (_ONE - self._mmr))
        else:
            price = (qty * pos.entry_price + pos.margin) / (qty * (_ONE + self._mmr))
        return price if price > 0 else None

    def _unrealized(self, pos: _PerpPosition, mark: Decimal | None) -> Decimal:
        if mark is None:
            return _ZERO
        return pos.qty * (mark - pos.entry_price)

    def _position_equity(self, pos: _PerpPosition, mark: Decimal | None) -> Decimal:
        return pos.margin + self._unrealized(pos, mark)

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
        """Market orders fill at once at the latest mark; other types rest against bars.

        OCO: ``price`` is the take-profit trigger, ``stop_price`` the stop-loss trigger;
        the pair is reduce-only with sibling-cancel (futures has no exchange OCO — this
        is the paper pair P4-D's ladder mirrors with TAKE_PROFIT_MARKET + STOP_MARKET).
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
        if self._opens(canon, s) or otype in (OrderType.LIMIT, OrderType.STOP_LIMIT):
            await self._require_persisted()

        if otype == OrderType.MARKET:
            mark, source = self._market_mark(canon)
            if mark is None or mark <= 0:
                raise ValueError(f"No market price available for {symbol}")
            open_qty = self._open_qty(canon, s, quantity)
            if source == "stale_live" and open_qty > 0:
                raise OrderRejectedError(
                    f"refusing a market {s} on {symbol}: the mark {mark} is stale "
                    f"(feed outage); reduce-only exits still fill, labelled"
                )
            fp = fill_rules.market_fill(s, mark, self._slippage_pct)
            self._check_rules(canon, quantity, fp.price)
            volume = self._bar_volume.get(canon)
            fillable_open = (
                open_qty
                if volume is None
                else fill_rules.participation_fillable(open_qty, volume, self._max_participation)
            )
            fill_qty = quantity - open_qty + fillable_open
            if fill_qty > 0:
                self._require_fill_margin(canon, s, fill_qty, fp)
                fill = self._apply_fill(
                    canon,
                    s,
                    OrderType.MARKET,
                    fill_qty,
                    fp,
                    "market",
                    str(uuid.uuid4()),
                    None,
                    self._bar_time(canon),
                    source,
                )
                if fillable_open < open_qty:
                    self._filled.append(
                        self._participation_cancel(
                            s,
                            OrderType.MARKET,
                            fill.symbol,
                            fill.price,
                            quantity - fill_qty,
                            fill.timestamp,
                        )
                    )
                    logger.warning(
                        "Perp market %s %s: volume %s filled %s of the %s opening; "
                        "the uncapped opening cancelled",
                        s,
                        symbol,
                        volume,
                        fillable_open,
                        open_qty,
                    )
                await self._persist(snapshot_at=fill.timestamp)
                resp = {**self._fill_response(fill), "mark_source": source}
                if fillable_open < open_qty:
                    resp["requested_quantity"] = float(quantity)
                    resp["participation_capped"] = True
                return resp
            # a bar with no volume absorbs nothing: the whole order is cancelled
            cancel = self._participation_cancel(
                s,
                OrderType.MARKET,
                self._name(canon),
                fp.price,
                quantity,
                self._bar_time(canon),
            )
            self._filled.append(cancel)
            logger.warning(
                "Perp market %s %s: the last bar's volume %s absorbs none of %s",
                s,
                symbol,
                volume,
                quantity,
            )
            await self._persist(snapshot_at=None)
            return {
                "id": cancel.order_id,
                "symbol": self._name(canon),
                "side": s,
                "type": OrderType.MARKET,
                "amount": float(quantity),
                "quantity": 0.0,
                "price": float(fp.price),
                "status": "CANCELLED",
                "filled": False,
                "reason": "participation_cap",
                "requested_quantity": float(quantity),
                "participation_capped": True,
            }

        if otype == OrderType.OCO:
            return await self._create_oco(canon, s, quantity, price, stop_price)
        if otype not in _RESTING_TYPES:
            raise OrderRejectedError(
                f"order type {order_type} is not supported by the perp paper executor"
            )
        order = self._new_resting(canon, s, otype, quantity, price, stop_price, params or {})
        self._pending.append(order)
        await self._persist(snapshot_at=None)
        return self._pending_response(order)

    def _opens(self, canon: str, side: str) -> bool:
        """Whether a *side* fill would open or increase exposure at the current position."""
        pos = self._positions.get(canon)
        if pos is None or pos.qty == 0:
            return True
        return (pos.qty > 0) == (side == "BUY")

    def _open_qty(self, canon: str, side: str, quantity: Decimal) -> Decimal:
        """The part of an order that opens or increases exposure (a flip opens its remainder)."""
        pos = self._positions.get(canon)
        if pos is None or pos.qty == _ZERO or (pos.qty > 0) == (side == "BUY"):
            return quantity
        return max(quantity - abs(pos.qty), _ZERO)

    def _require_fill_margin(self, canon: str, side: str, quantity: Decimal, fp: FillPrice) -> None:
        """Refuse upfront any fill whose open leg the wallet could not fund atomically.

        An opening or increasing fill posts its whole margin; a flip must fund only its
        open remainder, from the wallet the close leg releases — anything else leaves
        the ledger half-executed, so it raises before the first mutation.
        """
        pos = self._positions.get(canon)
        fee = quantity * fp.price * self._fee_rate(fp.liquidity)
        if pos is None or pos.qty == _ZERO or (pos.qty > 0) == (side == "BUY"):
            need = self._margin_needed(quantity * fp.price) + fee
            if self._wallet < need:
                raise InsufficientBalanceError(
                    f"Insufficient {self._quote} margin: need {need}, wallet {self._wallet}"
                )
            return
        reduce_qty = min(quantity, abs(pos.qty))
        remainder = quantity - reduce_qty
        if remainder <= 0:
            return
        share = reduce_qty / abs(pos.qty)
        gross = reduce_qty * (fp.price - pos.entry_price) * (_ONE if pos.qty > 0 else _MINUS_ONE)
        wallet_after = self._wallet + pos.margin * share + gross - fee * reduce_qty / quantity
        need = self._margin_needed(remainder * fp.price) + fee * remainder / quantity
        if wallet_after < need:
            raise InsufficientBalanceError(
                f"Insufficient {self._quote} margin: need {need} for the flip, "
                f"wallet {wallet_after} after the close"
            )

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
            reason = "stop_entry" if self._opens(canon, side) else "stop_loss"
        elif otype == OrderType.STOP_LIMIT:
            if price is None or stop_price is None or price <= 0 or stop_price <= 0:
                raise OrderRejectedError("STOP_LIMIT orders need a positive price and stop_price")
            ref = price
            reason = "stop_entry" if self._opens(canon, side) else "stop_loss"
        elif otype == OrderType.TAKE_PROFIT_MARKET:
            stop_price = stop_price if stop_price is not None else price
            price = None
            if stop_price is None or stop_price <= 0:
                raise OrderRejectedError("TAKE_PROFIT_MARKET orders need a positive trigger")
            ref = stop_price
            reason = "take_profit"
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
        reduce_only = bool(params.get("reduce_only"))
        self._order_seq += 1
        return _PendingOrder(
            order_id=str(uuid.uuid4()),
            symbol=canon,
            side=side,
            order_type=otype,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            seq=self._order_seq,
            reason=reason,
            created_after_ts=self._last_bar_ts.get(canon),
            reduce_only=reduce_only,
            trail_pct=trail_pct,
            extreme=extreme,
            placed_at=self._live_marks.get(canon),
        )

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
                "OCO orders require both price (take-profit trigger) "
                "and stop_price (stop-loss trigger)"
            )
        tp, sl = _d(price), _d(stop_price)
        if side == "SELL" and not sl < tp:
            raise OrderRejectedError(f"SELL OCO needs stop {sl} below take-profit {tp}")
        if side == "BUY" and not tp < sl:
            raise OrderRejectedError(f"BUY OCO needs take-profit {tp} below stop {sl}")
        self._check_rules(canon, quantity, min(tp, sl))
        oco_id = str(uuid.uuid4())
        created = self._last_bar_ts.get(canon)
        placed_at = self._live_marks.get(canon)
        legs: list[_PendingOrder] = []
        for otype, leg_stop, reason in (
            (OrderType.TAKE_PROFIT_MARKET, tp, "take_profit"),
            (OrderType.STOP_MARKET, sl, "stop_loss"),
        ):
            self._order_seq += 1
            legs.append(
                _PendingOrder(
                    order_id=str(uuid.uuid4()),
                    symbol=canon,
                    side=side,
                    order_type=otype,
                    quantity=quantity,
                    price=None,
                    stop_price=leg_stop,
                    seq=self._order_seq,
                    reason=reason,
                    created_after_ts=created,
                    reduce_only=True,
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
        """Cancel a resting order (an OCO by its id or either leg)."""
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

    # ------------------------------------------------------------------
    # Closed bars: resting orders, liquidation, funding
    # ------------------------------------------------------------------

    async def on_closed_bar(self, symbol: str, ohlcv: OHLCV) -> list[PaperFill]:
        """Evaluate this symbol's resting orders, liquidation and funding on one closed bar.

        The sequence is fixed: resting orders first, then liquidation, then funding, then
        the mark — a position closed by its stop cannot be liquidated in the same bar, and
        a liquidated position pays no funding for its final bar. Orders placed after the
        previous bar see only the path from their placement mark to the close. Each fill
        is capped to ``max_participation`` of the bar's volume; a resting order's
        remainder keeps working, same order id. Funding accruals are recorded on the
        account but are not order fills, so they are not returned here.
        """
        canon = self._symbol(symbol)
        last = self._last_bar_ts.get(canon)
        if last is not None and ohlcv.timestamp <= last:
            return []
        self._bar_volume[canon] = ohlcv.volume
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
            if self._halted is not None and not order.reduce_only:
                continue
            fill = self._evaluate(order, bar)
            if fill is not None:
                fills.append(fill)
        fills.extend(self._check_liquidation(canon, ohlcv))
        await self._accrue_funding(canon, last, ohlcv.timestamp)
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
            tp = next(o for o in legs if o.order_type == OrderType.TAKE_PROFIT_MARKET)
            sl = next(o for o in legs if o.order_type == OrderType.STOP_MARKET)
            if tp.stop_price is None or sl.stop_price is None:
                raise LedgerInvariantError(f"OCO {order.oco_id} is missing a leg price")
            stop_first = fill_rules.oco_stop_first(order.side, sl.stop_price, tp.stop_price, bar)
            if stop_first is None:
                return None
            fp = (
                fill_rules.stop_market_fill(order.side, sl.stop_price, bar, slip)
                if stop_first
                else _take_profit_fill(order.side, tp.stop_price, bar, slip)
            )
            if fp is None:
                raise LedgerInvariantError(f"OCO {order.oco_id} chose a leg it did not trigger")
            return self._fill_resting(sl if stop_first else tp, fp, bar.timestamp, bar.volume)

        if order.order_type == OrderType.LIMIT:
            if order.price is None:
                raise LedgerInvariantError(f"LIMIT {order.order_id} has no price")
            limit = fill_rules.limit_fill(order.side, order.price, bar)
            return (
                None
                if limit is None
                else self._fill_resting(order, limit, bar.timestamp, bar.volume)
            )
        if order.order_type == OrderType.STOP_MARKET:
            if order.stop_price is None:
                raise LedgerInvariantError(f"STOP_MARKET {order.order_id} has no stop")
            stop = fill_rules.stop_market_fill(order.side, order.stop_price, bar, slip)
            return (
                None if stop is None else self._fill_resting(order, stop, bar.timestamp, bar.volume)
            )
        if order.order_type == OrderType.STOP_LIMIT:
            if order.stop_price is None or order.price is None:
                raise LedgerInvariantError(f"STOP_LIMIT {order.order_id} has no prices")
            sl_outcome = fill_rules.stop_limit_fill(order.side, order.stop_price, order.price, bar)
            if sl_outcome is None:
                return None
            if sl_outcome.fill is not None:
                return self._fill_resting(order, sl_outcome.fill, bar.timestamp, bar.volume)
            order.order_type = OrderType.LIMIT
            order.stop_price = None
            return None
        if order.order_type == OrderType.TAKE_PROFIT_MARKET:
            if order.stop_price is None:
                raise LedgerInvariantError(f"TAKE_PROFIT_MARKET {order.order_id} has no trigger")
            profit = _take_profit_fill(order.side, order.stop_price, bar, slip)
            return (
                None
                if profit is None
                else self._fill_resting(order, profit, bar.timestamp, bar.volume)
            )
        if order.extreme is None or order.trail_pct is None:
            raise LedgerInvariantError(f"TRAILING_STOP {order.order_id} has no peak")
        trail = fill_rules.trailing_fill(order.side, order.extreme, order.trail_pct, bar, slip)
        order.extreme = trail.extreme
        return (
            None
            if trail.fill is None
            else self._fill_resting(order, trail.fill, bar.timestamp, bar.volume)
        )

    def _fill_resting(
        self, order: _PendingOrder, fp: FillPrice, ts: datetime, volume: Decimal
    ) -> PaperFill | None:
        """Fill one bar's slice of a resting order; the remainder keeps working, same id.

        The slice is capped to ``max_participation`` of the bar's volume and a
        reduce-only slice never exceeds the position left. The wallet check stays
        atomic on the whole remaining order — a flip whose open leg the wallet cannot
        fund cancels untouched, never half-executes its close leg slice by slice.
        A partial OCO leg fill drops its sibling and continues standalone — a
        documented deviation (Binance cancels the whole list).
        """
        held = _ZERO
        check_qty = order.quantity
        if order.reduce_only:
            pos = self._positions.get(order.symbol)
            held = _ZERO if pos is None else abs(pos.qty)
            check_qty = min(order.quantity, held)
            if check_qty <= 0:
                # nothing left to reduce (the position was liquidated under it): the
                # order is dropped with a CANCELLED record so tracking sees it go
                self._remove_order(order)
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
                    reason="position_gone",
                    timestamp=ts,
                    fill_seq=0,
                    status="CANCELLED",
                )
        parent = order.oco_id
        try:
            self._require_fill_margin(order.symbol, order.side, check_qty, fp)
        except InsufficientBalanceError as exc:
            self._remove_order(order)
            logger.warning(
                "Perp %s %s cancelled: fill at %s %s",
                order.order_type,
                order.order_id,
                fp.price,
                exc,
            )
            return PaperFill(
                order_id=order.order_id,
                parent_order_id=parent,
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
                reason="insufficient_margin_on_gap",
                timestamp=ts,
                fill_seq=0,
                status="CANCELLED",
            )
        slice_qty = fill_rules.participation_fillable(check_qty, volume, self._max_participation)
        if slice_qty <= 0:
            return None
        # a reduce-only slice that closes the position takes the dead remainder with it
        final = slice_qty >= order.quantity or (order.reduce_only and slice_qty >= held)
        if final:
            self._remove_order(order)
        else:
            if parent is not None:
                self._detach_oco(order)
            order.quantity -= slice_qty
        return self._apply_fill(
            order.symbol,
            order.side,
            order.order_type,
            slice_qty,
            fp,
            order.reason,
            order.order_id,
            parent,
            ts,
        )

    def _detach_oco(self, order: _PendingOrder) -> None:
        """A partial OCO leg fill: drop the sibling and go standalone (no locks to keep)."""
        self._pending = [o for o in self._pending if o.oco_id != order.oco_id or o is order]
        order.oco_id = None

    def _participation_cancel(
        self,
        side: str,
        order_type: str,
        name: str,
        price: Decimal,
        remainder: Decimal,
        ts: datetime,
    ) -> PaperFill:
        """A market order's uncapped opening remainder: nothing traded, cancelled."""
        return PaperFill(
            order_id=str(uuid.uuid4()),
            parent_order_id=None,
            symbol=name,
            side=side,
            order_type=order_type,
            quantity=remainder,
            price=price,
            fee=_ZERO,
            fee_currency=self._quote,
            liquidity="taker",
            gross_pnl=None,
            net_pnl=None,
            reason="participation_cap",
            timestamp=ts,
            fill_seq=0,
            status="CANCELLED",
        )

    def _check_liquidation(self, canon: str, bar: OHLCV) -> list[PaperFill]:
        pos = self._positions.get(canon)
        if pos is None or pos.qty == 0:
            return []
        price = self._liquidation_price(pos)
        if price is None:
            return []
        long = pos.qty > 0
        hit = bar.low <= price if long else bar.high >= price
        if not hit:
            return []
        return [self._force_liquidation(canon, pos, price, bar.timestamp)]

    def _force_liquidation(
        self, canon: str, pos: _PerpPosition, price: Decimal, ts: datetime
    ) -> PaperFill:
        """Close the position at *price*, lose its margin, record the fill and halt.

        The ledger stays consistent, so this halt persists (a broken-ledger halt does not).
        """
        long = pos.qty > 0
        qty = abs(pos.qty)
        gross = qty * (price - pos.entry_price) * (_ONE if long else _MINUS_ONE)
        remaining = pos.margin + gross  # the maintenance margin at the liquidation price
        fee = remaining if remaining > 0 else _ZERO
        net = gross - fee
        margin_lost = pos.margin
        pos.realized_net += net
        pos.qty = _ZERO
        pos.margin = _ZERO
        pos.entry_fees_open = _ZERO
        pos.entry_price = _ZERO
        self._liquidations += 1
        self._realized_gross += gross
        self._realized_net += net
        self._fees_paid += fee
        self._fill_seq += 1
        fill = PaperFill(
            order_id=str(uuid.uuid4()),
            parent_order_id=None,
            symbol=self._name(canon),
            side="SELL" if long else "BUY",
            order_type="LIQUIDATION",
            quantity=qty,
            price=price,
            fee=fee,
            fee_currency=self._quote,
            liquidity="taker",
            gross_pnl=gross,
            net_pnl=net,
            reason="liquidation",
            timestamp=ts,
            fill_seq=self._fill_seq,
        )
        self._filled.append(fill)
        if self._db_pool is not None:
            self._outbox.append(fill)
        self._halt(
            f"{self._name(canon)} position liquidated at {price} "
            f"(leverage {self._leverage}x, margin lost {margin_lost})",
            broken=False,
        )
        return fill

    async def _accrue_funding(self, canon: str, last: datetime | None, bar_ts: datetime) -> None:
        """Apply the settled funding rows of ``(last, bar_ts]`` to the open position.

        The notional is valued at the mark carried into the bar (the last closed bar's
        close, or the entry price before any bar closed) — never a price from inside the
        bar, so a live mark between bars cannot move the accrual. No rows for a window
        that crossed a settlement boundary counts as missing; no rate is ever fabricated.
        Accruals are recorded as ``FUNDING`` fills on the account only — they are not
        order fills and publish no events. Isolated margin caps every debit at the
        position's equity: a payment that would cross the maintenance boundary liquidates
        the position at that event, at the carried price, so the margin is never carried
        negative and the loss is exactly the margin.
        """
        pos = self._positions.get(canon)
        if pos is None or pos.qty == 0:
            return
        rows: Sequence[FundingRow] = ()
        if self._funding_reader is not None and last is not None:
            try:
                rows = await self._funding_reader(self._name(canon), last, bar_ts)
            except Exception:
                logger.exception(
                    "Funding rows for %s could not be read; the window is skipped",
                    self._name(canon),
                )
                rows = ()
        if not rows:
            if _settlement_boundary_in(last, bar_ts):
                self._funding_missing += 1
            return
        mark = self._marks.get(canon)
        price = mark if mark is not None and mark > 0 else pos.entry_price
        for settled_at, rate in rows:
            if rate == _ZERO:
                continue
            # longs pay a positive rate, shorts receive it: cash flow = -qty * rate * price
            cash_flow = -(pos.qty * rate * price)
            payable = max(min(pos.margin, self._position_equity(pos, price)), _ZERO)
            if cash_flow < -payable:
                cash_flow = -payable
            pos.margin += cash_flow
            pos.funding_paid -= cash_flow
            self._funding_applied += 1
            self._fill_seq += 1
            fill = PaperFill(
                order_id=str(uuid.uuid4()),
                parent_order_id=None,
                symbol=self._name(canon),
                side="FUNDING",
                order_type="FUNDING",
                quantity=abs(pos.qty),
                price=price,
                fee=-cash_flow if cash_flow < 0 else _ZERO,
                fee_currency=self._quote,
                liquidity="taker",
                gross_pnl=None,
                net_pnl=cash_flow,
                reason="funding",
                timestamp=settled_at,
                fill_seq=self._fill_seq,
            )
            self._filled.append(fill)
            if self._db_pool is not None:
                self._outbox.append(fill)
            if self._position_equity(pos, price) <= self._mmr * abs(pos.qty) * price:
                self._force_liquidation(canon, pos, price, settled_at)
                return

    # ------------------------------------------------------------------
    # Fills and the margin ledger
    # ------------------------------------------------------------------

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
        mark_source: str | None = None,
    ) -> PaperFill:
        self._require_fill_margin(canon, side, quantity, fp)
        pos = self._positions.setdefault(canon, _PerpPosition())
        signed = _ONE if side == "BUY" else _MINUS_ONE
        delta = signed * quantity
        fee = quantity * fp.price * self._fee_rate(fp.liquidity)
        gross: Decimal | None = None
        net: Decimal | None = None
        reducing = pos.qty != _ZERO and (pos.qty > 0) != (delta > 0)
        if reducing:
            reduce_qty = min(quantity, abs(pos.qty))
            share = reduce_qty / abs(pos.qty)
            gross = (
                reduce_qty * (fp.price - pos.entry_price) * (_ONE if pos.qty > 0 else _MINUS_ONE)
            )
            fee_exit = fee * reduce_qty / quantity
            margin_out = pos.margin * share
            entry_fee_out = pos.entry_fees_open * share
            net = gross - fee_exit - entry_fee_out
            self._wallet += margin_out + gross - fee_exit
            pos.qty += signed * reduce_qty
            pos.margin -= margin_out
            pos.entry_fees_open -= entry_fee_out
            pos.realized_net += net
            self._realized_gross += gross
            self._realized_net += net
            if pos.qty == _ZERO:
                self._wallet += pos.margin  # pro-rata dust returns to the wallet
                pos.margin = _ZERO
            remainder = quantity - reduce_qty
            if remainder > 0:
                self._open_part(canon, side, remainder, fp, fee * (remainder / quantity))
        else:
            self._open_part(canon, side, quantity, fp, fee)
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
            mark_source=cast(Literal["live", "stale_live", "last_close"], mark_source),
        )
        self._filled.append(fill)
        if self._db_pool is not None:
            self._outbox.append(fill)
        try:
            self._check_invariants()
        except LedgerInvariantError as exc:
            if self._halted is not None:
                logger.warning("Perp session %s exit on a halted ledger: %s", self._session_id, exc)
            else:
                self._halt(f"ledger invariant broken: {exc}")
                raise
        self._update_peak()
        return fill

    def _open_part(
        self, canon: str, side: str, quantity: Decimal, fp: FillPrice, fee: Decimal
    ) -> None:
        """Open or extend the position by *quantity* at the fill price (margin checked)."""
        pos = self._positions.setdefault(canon, _PerpPosition())
        notional = quantity * fp.price
        margin = self._margin_needed(notional)
        need = margin + fee
        if self._wallet < need:
            raise InsufficientBalanceError(
                f"Insufficient {self._quote} margin: need {need}, wallet {self._wallet}"
            )
        self._wallet -= need
        if pos.qty == _ZERO or (pos.qty > 0) == (side == "BUY"):
            total_qty = abs(pos.qty) + quantity
            pos.entry_price = (abs(pos.qty) * pos.entry_price + notional) / total_qty
            pos.qty += (_ONE if side == "BUY" else _MINUS_ONE) * quantity
            pos.margin += margin
            pos.entry_fees_open += fee

    def _check_invariants(self) -> None:
        if self._wallet < 0:
            raise LedgerInvariantError(f"negative wallet {self._wallet}")
        for canon, pos in self._positions.items():
            if pos.margin < 0 or pos.entry_fees_open < 0:
                raise LedgerInvariantError(f"negative margin for {canon}")
            if pos.qty == _ZERO and (pos.margin != _ZERO or pos.entry_fees_open != _ZERO):
                raise LedgerInvariantError(f"flat position {canon} still holds margin")
        for order in self._pending:
            if order.oco_id is None and order.order_type not in _RESTING_TYPES:
                raise LedgerInvariantError(f"order {order.order_id} has a bad type")

    # ------------------------------------------------------------------
    # Flatten
    # ------------------------------------------------------------------

    async def flatten(self, symbol: str | None = None, reason: str = "flatten") -> list[PaperFill]:
        """Cancel resting orders and market-close every position (shorts by BUY-back)."""
        if symbol is not None:
            targets = {self._symbol(symbol)}
        else:
            targets = {o.symbol for o in self._pending} | {
                c for c, p in self._positions.items() if p.qty != _ZERO
            }
        fills: list[PaperFill] = []
        for canon in sorted(targets):
            for order in [o for o in self._pending if o.symbol == canon]:
                if order in self._pending:
                    self._remove_order(order)
            pos = self._positions.get(canon)
            if pos is None or pos.qty == _ZERO:
                continue
            mark, source = self._market_mark(canon)
            if mark is None or mark <= 0:
                raise ValueError(f"cannot flatten {canon}: no market price for {abs(pos.qty)} held")
            side = "SELL" if pos.qty > 0 else "BUY"
            fp = fill_rules.market_fill(side, mark, self._slippage_pct)
            fills.append(
                self._apply_fill(
                    canon,
                    side,
                    OrderType.MARKET,
                    abs(pos.qty),
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

    def _margin_used(self) -> Decimal:
        return sum((p.margin for p in self._positions.values()), _ZERO)

    def _equity(self) -> Decimal:
        total = self._wallet
        for canon, pos in self._positions.items():
            if pos.qty != _ZERO:
                total += self._position_equity(pos, self._marks.get(canon))
        return total

    def _unrealized_total(self) -> Decimal:
        return sum(
            (
                self._unrealized(p, self._marks.get(c))
                for c, p in self._positions.items()
                if p.qty != _ZERO
            ),
            _ZERO,
        )

    def _update_peak(self) -> None:
        self._peak_equity = max(self._peak_equity, self._equity())

    def _drawdown(self, equity: Decimal) -> Decimal:
        if self._peak_equity <= 0:
            return _ZERO
        return max(_ZERO, (self._peak_equity - equity) / self._peak_equity)

    async def get_equity(self) -> Decimal:
        """Wallet plus every position's equity at its last mark (ExecutorState protocol)."""
        return self._equity()

    def get_account_snapshot(self) -> PerpAccountSnapshot:
        equity = self._equity()
        return PerpAccountSnapshot(
            quote_asset=self._quote,
            wallet_available=self._wallet,
            margin_used=self._margin_used(),
            base_totals={self._base(c): abs(p.qty) for c, p in sorted(self._positions.items())},
            marks=dict(self._marks),
            equity=equity,
            initial_equity=self._initial_equity,
            realized_gross=self._realized_gross,
            fees_paid=self._fees_paid,
            realized_net=self._realized_net,
            unrealized=self._unrealized_total(),
            peak_equity=self._peak_equity,
            drawdown=self._drawdown(equity),
            open_orders=len(self._pending),
            last_bar_ts=dict(self._last_bar_ts),
            persist_dirty=self._dirty,
            halted=self._halted,
            leverage=self._leverage,
            funding_applied=self._funding_applied,
            funding_missing=self._funding_missing,
            liquidations=self._liquidations,
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        if self._session_id:
            metrics.set_paper_persist_dirty(self._session_id, dirty)

    def _halt(self, reason: str, *, broken: bool = True) -> None:
        """Halt the session; a broken-ledger halt never persists, a terminal one does."""
        if self._halted is None:
            logger.error("Perp session %s halted: %s", self._session_id, reason)
            self._halted = reason
            self._halt_broken = broken

    def _require_running(self) -> None:
        if self._halted is not None:
            raise PaperSessionHaltedError(
                f"perp session {self._session_id} is halted ({self._halted}); "
                "only flatten() still runs"
            )

    async def _require_persisted(self) -> None:
        if not self._dirty:
            return
        await self._persist(snapshot_at=None)
        if self._dirty:
            raise PersistenceUnavailableError(
                f"perp session {self._session_id} has {len(self._outbox)} unpersisted fills; "
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
        if self._halted is not None and self._halt_broken:
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
        for canon, pos in sorted(self._positions.items()):
            if pos.qty != _ZERO:
                positions.append(
                    paper_store.PositionRow(
                        symbol=self._name(canon),
                        quantity=abs(pos.qty),
                        avg_entry_price=pos.entry_price,
                        unrealized_pnl=self._unrealized(pos, self._marks.get(canon)),
                        realized_pnl=pos.realized_net,
                        direction="LONG" if pos.qty > 0 else "SHORT",
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
                unrealized_pnl=self._unrealized_total(),
                realized_pnl=self._realized_net,
                drawdown_pct=self._drawdown(equity),
                peak_value=self._peak_equity,
            )
        try:
            async with self._db_pool.acquire() as conn:
                await paper_store.save(
                    conn, account, trades, positions, flat, snapshot, source=self._source
                )
        except paper_store.StaleStateError:
            if await self._adopt_landed_write():
                await self._write(snapshot_at)
            return
        except Exception:
            logger.exception(
                "Perp state for session %s not persisted; %d fills held in memory",
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
        """After a refused write: adopt the stored version if it is one of our failed writes."""
        try:
            async with self._db_pool.acquire() as conn:
                stored = await paper_store.load(conn, self._session_id)
        except Exception:
            logger.exception("Perp state for session %s could not be re-read", self._session_id)
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
            "max_participation": str(self._max_participation),
            "step_size": _opt_s(self._step_size),
            "min_notional": _opt_s(self._min_notional),
            "leverage": self._leverage,
            "maintenance_margin_rate": str(self._mmr),
            "wallet": str(self._wallet),
            "positions": {
                c: {
                    "qty": str(p.qty),
                    "entry_price": str(p.entry_price),
                    "margin": str(p.margin),
                    "entry_fees_open": str(p.entry_fees_open),
                    "realized_net": str(p.realized_net),
                    "funding_paid": str(p.funding_paid),
                }
                for c, p in sorted(self._positions.items())
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
                    "seq": o.seq,
                    "reason": o.reason,
                    "created_after_ts": (
                        None if o.created_after_ts is None else _ts(o.created_after_ts)
                    ),
                    "reduce_only": o.reduce_only,
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
            "funding_applied": self._funding_applied,
            "funding_missing": self._funding_missing,
            "liquidations": self._liquidations,
            "halted": self._halted,
            "halt_broken": self._halt_broken,
        }

    @classmethod
    def from_state(
        cls,
        state: Mapping[str, Any],
        *,
        exchange_id: ExchangeId = PERP_EXCHANGE_ID,
        db_pool: Any = None,
        strategy_id: str = "",
        session_id: str = "",
        market_rules: Mapping[str, SpotMarketRules] | None = None,
        funding_reader: FundingReader | None = None,
    ) -> PerpPaperTradingExecutor:
        """Rebuild a perp executor from ``to_state``; raises PaperStateCorruptError if invalid."""
        try:
            if state.get("format") != _STATE_FORMAT:
                raise ValueError(f"unknown perp state format {state.get('format')!r}")
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
                max_participation=_d(state.get("max_participation", DEFAULT_MAX_PARTICIPATION)),
                step_size=_opt_d(state["step_size"]),
                min_notional=_opt_d(state["min_notional"]),
                market_rules=market_rules,
                leverage=int(state["leverage"]),
                maintenance_margin_rate=_d(state["maintenance_margin_rate"]),
                funding_reader=funding_reader,
            )
            ex._rebuild_from_state(state)
        except (
            KeyError,
            TypeError,
            ValueError,
            ArithmeticError,
            LedgerInvariantError,
            UnsupportedVenueError,
        ) as exc:
            raise PaperStateCorruptError(f"perp state for session {session_id!r}: {exc}") from exc
        return ex

    def _rebuild_from_state(self, state: Mapping[str, Any]) -> None:
        self._wallet = _d(state["wallet"])
        self._positions = {
            c: _PerpPosition(
                qty=_d(v["qty"]),
                entry_price=_d(v["entry_price"]),
                margin=_d(v["margin"]),
                entry_fees_open=_d(v["entry_fees_open"]),
                realized_net=_d(v["realized_net"]),
                funding_paid=_d(v["funding_paid"]),
            )
            for c, v in state["positions"].items()
        }
        self._pending = [
            _PendingOrder(
                order_id=str(o["order_id"]),
                symbol=str(o["symbol"]),
                side=str(o["side"]),
                order_type=str(o["order_type"]),
                quantity=_d(o["quantity"]),
                price=_opt_d(o["price"]),
                stop_price=_opt_d(o["stop_price"]),
                seq=int(o["seq"]),
                reason=str(o["reason"]),
                created_after_ts=(
                    None if o["created_after_ts"] is None else _parse_ts(o["created_after_ts"])
                ),
                reduce_only=bool(o.get("reduce_only", False)),
                trail_pct=_opt_d(o["trail_pct"]),
                extreme=_opt_d(o["extreme"]),
                oco_id=o["oco_id"],
                placed_at=_opt_d(o.get("placed_at")),
            )
            for o in state["pending"]
        ]
        self._marks = {c: _d(v) for c, v in state["marks"].items()}
        self._last_bar_ts = {c: _parse_ts(v) for c, v in state["last_bar_ts"].items()}
        self._names = {str(k): str(v) for k, v in state["names"].items()}
        self._fill_seq = int(state["fill_seq"])
        self._order_seq = int(state["order_seq"])
        self._initial_equity = _d(state["initial_equity"])
        self._peak_equity = _d(state["peak_equity"])
        self._realized_gross = _d(state["realized_gross"])
        self._fees_paid = _d(state["fees_paid"])
        self._realized_net = _d(state["realized_net"])
        self._funding_applied = int(state.get("funding_applied", 0))
        self._funding_missing = int(state.get("funding_missing", 0))
        self._liquidations = int(state.get("liquidations", 0))
        halted = state.get("halted")
        self._halted = None if halted is None else str(halted)
        self._halt_broken = bool(state.get("halt_broken", False))
        self._check_invariants()

    @classmethod
    async def restore(
        cls,
        db_pool: Any,
        session_id: str,
        *,
        market_rules: Mapping[str, SpotMarketRules] | None = None,
        funding_reader: FundingReader | None = None,
    ) -> PerpPaperTradingExecutor | None:
        """Rebuild the session's perp account from ``paper_accounts`` (spot's semantics)."""
        stored, orphaned, trades = await read_stored_account(db_pool, session_id)
        if stored is None:
            if orphaned:
                raise PaperStateUnavailableError(
                    f"perp state unavailable (pre-P4 session) for {session_id!r}: "
                    "open positions exist without saved perp state"
                )
            return None
        if stored.exchange_id != PERP_EXCHANGE_ID:
            raise PaperStateCorruptError(
                f"perp restore for {session_id!r}: row exchange {stored.exchange_id!r} "
                f"is not {PERP_EXCHANGE_ID}"
            )
        ex = cls.from_state(
            stored.state,
            exchange_id=PERP_EXCHANGE_ID,
            db_pool=db_pool,
            strategy_id=stored.strategy_id,
            session_id=stored.session_id,
            market_rules=market_rules,
            funding_reader=funding_reader,
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
            # a perp opening fill can be a SELL (short entry): net_pnl NULL marks it
            gross_pnl=None if t.net_pnl is None else t.gross_pnl,
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
            "status": fill.status,
            "filled": fill.status == "FILLED",
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
        """Quote cash net of borrowing (ExecutorState protocol).

        The account read adds every position's signed value at the mark on top, so the
        balance leg is the margin-trading cash: the wallet plus each position's margin
        minus its entry notional (a leveraged long has borrowed the rest; at 1x this is
        exactly the wallet). Wallet + margins + unrealized then equals ``get_equity()``.
        """
        cash = self._wallet + sum(
            (p.margin - p.qty * p.entry_price for p in self._positions.values() if p.qty != _ZERO),
            _ZERO,
        )
        return {self._quote: cash}

    async def fetch_balance(self) -> dict[str, Decimal]:
        """Free margin (the wallet); locked margin is excluded, like ccxt."""
        return {self._quote: self._wallet}

    def _open_positions(self, symbol: str | None) -> list[tuple[str, _PerpPosition]]:
        wanted = None if symbol is None else _canon_symbol(symbol)
        return [
            (c, p)
            for c, p in sorted(self._positions.items())
            if p.qty != _ZERO and (wanted is None or c == wanted)
        ]

    async def fetch_positions(self, symbol: str | None = None) -> list[dict[str, Any]]:
        """Open positions in ccxt shape (side long/short, signed contracts)."""
        return [
            {
                "symbol": self._name(c),
                "side": "long" if p.qty > 0 else "short",
                "contracts": float(p.qty),
                "entryPrice": float(p.entry_price),
                "unrealizedPnl": float(self._unrealized(p, self._marks.get(c))),
                "realizedPnl": float(p.realized_net),
                "liquidationPrice": _opt_float(self._liquidation_price(p)),
            }
            for c, p in self._open_positions(symbol)
        ]

    async def get_positions(self, symbol: str | None = None) -> list[Position]:
        """Open positions as Position objects (ExecutorState protocol); realized is net."""
        return [
            Position(
                symbol=Symbol(self._name(c)),
                direction=Direction.LONG if p.qty > 0 else Direction.SHORT,
                quantity=abs(p.qty),
                avg_entry_price=p.entry_price,
                unrealized_pnl=self._unrealized(p, self._marks.get(c)),
                realized_pnl=p.realized_net,
                strategy_id=self._strategy_id,
                exchange_id=self._exchange_id,
            )
            for c, p in self._open_positions(symbol)
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
                "reduce_only": o.reduce_only,
            }
            for o in sorted(self._pending, key=lambda o: o.seq)
            if wanted is None or o.symbol == wanted
        ]


def _opt_float(price: Decimal | None) -> float | None:
    return None if price is None else float(price)


def _stop_trigger(o: _PendingOrder) -> float | None:
    """The stop's trigger; a trailing stop's moves with the peak (SELL) or trough (BUY)."""
    if o.extreme is not None and o.trail_pct is not None:
        return float(o.extreme * (_ONE - o.trail_pct if o.side == "SELL" else _ONE + o.trail_pct))
    return None if o.stop_price is None else float(o.stop_price)


def _state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(paper_store.dumps_state(state).encode()).hexdigest()


PAPER_EXECUTORS: tuple[type[Any], ...] = (PaperTradingExecutor, PerpPaperTradingExecutor)
"""Every executor whose ``get_account_snapshot`` describes a paper account (detail reads)."""

PaperExecutor = PaperTradingExecutor | PerpPaperTradingExecutor
"""Either paper executor: spot ledger or perp margin wallet (the runtime accepts both)."""
