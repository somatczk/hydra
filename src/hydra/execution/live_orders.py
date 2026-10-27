"""The live order state machine: every live order's intent, ack and outcome, DB-persisted.

``create`` writes the PENDING_SUBMIT intent BEFORE the exchange call (outbox-first), so a
crash mid-submit never loses the possibility of an order resting on the exchange. No code
path ever resubmits an unsettled row: the poller resolves it by ``client_order_id`` instead.

Transitions (seam #2 status vocabulary pending_submit|submitting|new|partially_filled|filled|
canceled|expired|rejected|unknown):

* ``mark_ack`` — the create response arrived. Idempotent from pending_submit/submitting only;
  an ack racing a poller-resolved row is refused and reported as False, never an error (R16).
* ``mark_submit_failed`` — a definitive exchange rejection: the order is known not to rest.
* ``resolve_unknown`` — fetch_order answered for an unsettled row (pending_submit, submitting
  or unknown): it sets the exchange id and status in one step.
* ``apply_status`` — monotonic: a terminal status is never left, ``filled_quantity`` never
  decreases, and a same-status, same-quantity re-apply is the no-op ``unchanged``. One
  conditional UPDATE at the DB: the WHERE clause is the invariant, so no read-plan-write
  race can regress a terminal row (F7).
* ``record_fill`` — the CLAIM of the claim-apply-confirm pipeline (F2): the INSERT with
  ``applied=false`` and ``ON CONFLICT (exchange_id, symbol, trade_id) DO NOTHING``; True only
  when newly claimed. ``unapplied_fills`` lists the rows a later sweep must re-apply before
  claiming new ones; ``mark_fill_applied`` is the confirm.

Every order lookup the fill collector uses is session-scoped (F1): a trade of another
session's order on the same account is R5-filtered, never recorded. ``order_list_id`` is the
F12 discriminator: non-NULL means this row's ``exchange_order_id`` is a Binance order LIST
id (an OCO), so ``by_exchange_order_id`` (the trade-order-id lookup) never matches a list id
to an order row — ``by_order_list_id`` is the OCO lookup.

``client_order_id`` is hydra's own ``newClientOrderId`` on the wire: ``hyd-`` + a uuid4 hex
(36 characters, Binance's limit). The prefix is the ownership marker live adopt gates on
(R5: the user's orders are never hijacked). An OCO additionally carries ``listClientOrderId``
(the row's own id) and two explicit ``aboveClientOrderId``/``belowClientOrderId`` leg ids,
persisted in ``leg_client_order_ids`` so the poller can resolve the legs (F9).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol, cast

logger = logging.getLogger(__name__)

CLIENT_ORDER_ID_PREFIX = "hyd-"

APPLIED = "applied"
UNCHANGED = "unchanged"
REFUSED = "refused"


def new_client_order_id() -> str:
    """A hydra-owned Binance newClientOrderId: the prefix plus a uuid4 hex (36 chars)."""
    return f"{CLIENT_ORDER_ID_PREFIX}{uuid.uuid4().hex}"


class LiveOrderStatus(StrEnum):
    PENDING_SUBMIT = "pending_submit"
    SUBMITTING = "submitting"
    NEW = "new"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


TERMINAL_STATUSES = frozenset(
    {
        LiveOrderStatus.FILLED,
        LiveOrderStatus.CANCELED,
        LiveOrderStatus.EXPIRED,
        LiveOrderStatus.REJECTED,
    }
)
UNSETTLED_STATUSES = frozenset(
    {
        LiveOrderStatus.PENDING_SUBMIT,
        LiveOrderStatus.SUBMITTING,
        LiveOrderStatus.UNKNOWN,
    }
)
_ACKABLE = frozenset({LiveOrderStatus.PENDING_SUBMIT, LiveOrderStatus.SUBMITTING})


def map_ccxt_status(raw: Any, filled: Any = None) -> LiveOrderStatus:
    """A raw Binance/ccxt order status -> the seam #2 vocabulary; unrecognized is unknown."""
    status = str(raw or "").upper()
    if status in ("PARTIALLY_FILLED", "PARTIAL", "PENDING_CANCEL", "PENDING"):
        return LiveOrderStatus.PARTIALLY_FILLED
    if status in ("FILLED", "CLOSED"):
        return LiveOrderStatus.FILLED
    if status in ("CANCELED", "CANCELLED"):
        return LiveOrderStatus.CANCELED
    if status == "EXPIRED":
        return LiveOrderStatus.EXPIRED
    if status in ("REJECTED", "PENDING_REJECT"):
        return LiveOrderStatus.REJECTED
    if status in ("NEW", "PENDING_NEW", "OPEN", "PROCESS"):
        return LiveOrderStatus.PARTIALLY_FILLED if _positive(filled) else LiveOrderStatus.NEW
    return LiveOrderStatus.UNKNOWN


def _positive(value: Any) -> bool:
    try:
        return Decimal(str(value)) > 0
    except (ArithmeticError, ValueError, TypeError):
        return False


def _dec(value: Any) -> Decimal:
    return Decimal(str(value))


def _dec_opt(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _ts(value: Any) -> datetime | None:
    return None if value is None else value


def _refused(order: LiveOrderRecord, what: str) -> str:
    logger.info("Refused %s for %s: status is %s", what, order.client_order_id, order.status.value)
    return REFUSED


def _terminal_now(status: LiveOrderStatus) -> datetime | None:
    return datetime.now(UTC) if status in TERMINAL_STATUSES else None


def _plan_status(
    order: LiveOrderRecord,
    status: LiveOrderStatus,
    filled_quantity: Decimal | None,
    average_fill_price: Decimal | None,
    error: str | None,
) -> tuple[LiveOrderRecord | None, str]:
    """The monotonic apply_status rules, shared by both stores; None when nothing is written."""
    if order.status in TERMINAL_STATUSES:
        return None, _refused(order, "status change")
    if filled_quantity is not None and filled_quantity < order.filled_quantity:
        logger.info(
            "Refused filled_quantity %s -> %s for %s",
            order.filled_quantity,
            filled_quantity,
            order.client_order_id,
        )
        return None, REFUSED
    if (
        status == order.status
        and (filled_quantity is None or filled_quantity == order.filled_quantity)
        and error is None
    ):
        return None, UNCHANGED
    updated = replace(
        order,
        status=status,
        filled_quantity=order.filled_quantity if filled_quantity is None else filled_quantity,
        average_fill_price=(
            order.average_fill_price if average_fill_price is None else average_fill_price
        ),
        error=error,
        terminal_at=datetime.now(UTC) if status in TERMINAL_STATUSES else None,
        updated_at=datetime.now(UTC),
    )
    return updated, APPLIED


@dataclass(frozen=True, slots=True)
class LiveOrderRecord:
    """One live order row; ``exchange_order_id`` is the orderListId for an OCO.

    ``order_list_id`` is the F12 discriminator (non-NULL: the row is an OCO list);
    ``leg_client_order_ids`` are the wire ``aboveClientOrderId``/``belowClientOrderId``.
    """

    client_order_id: str
    session_id: str
    strategy_id: str
    exchange_id: str
    symbol: str
    side: str
    order_type: str
    quantity: Decimal
    status: LiveOrderStatus
    price: Decimal | None = None
    stop_price: Decimal | None = None
    filled_quantity: Decimal = Decimal("0")
    average_fill_price: Decimal | None = None
    fee: Decimal | None = None
    fee_currency: str | None = None
    reduce_only: bool = False
    tag: str = ""
    actor: str = "engine"
    exchange_order_id: str | None = None
    order_list_id: str | None = None
    leg_client_order_ids: tuple[str, ...] = ()
    submitted_at: datetime | None = None
    terminal_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class LiveFillRecord:
    """One exchange trade; (exchange_id, symbol, trade_id) is the replay guard."""

    trade_id: str
    client_order_id: str
    session_id: str
    exchange_id: str
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    fee_currency: str
    liquidity: str
    timestamp: datetime


class LiveOrderStore(Protocol):
    """The live-order persistence contract; the asyncpg and in-memory twins are interchangeable."""

    async def create(self, order: LiveOrderRecord) -> None: ...

    async def mark_ack(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        order_list_id: str | None = None,
    ) -> bool: ...

    async def mark_submit_failed(self, client_order_id: str, error: str) -> bool: ...

    async def resolve_unknown(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        order_list_id: str | None = None,
    ) -> bool: ...

    async def apply_status(
        self,
        client_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        error: str | None = None,
    ) -> str: ...

    async def record_fill(self, fill: LiveFillRecord) -> bool: ...

    async def unapplied_fills(self, session_id: str) -> list[LiveFillRecord]: ...

    async def mark_fill_applied(self, fill: LiveFillRecord) -> bool: ...

    async def open_orders(self, session_id: str) -> list[LiveOrderRecord]: ...

    async def orders(self, session_id: str, limit: int = 100) -> list[LiveOrderRecord]: ...

    async def by_client_order_id(self, client_order_id: str) -> LiveOrderRecord | None: ...

    async def by_exchange_order_id(
        self, exchange_order_id: str, session_id: str
    ) -> LiveOrderRecord | None: ...

    async def by_order_list_id(
        self, order_list_id: str, session_id: str
    ) -> LiveOrderRecord | None: ...

    async def tracked_by_exchange_order_id(self, exchange_order_id: str) -> bool: ...

    async def tracked_by_order_list_id(self, order_list_id: str) -> bool: ...

    async def unsettled(self, session_id: str, older_than: datetime) -> list[LiveOrderRecord]: ...

    async def refreshable(self, session_id: str) -> list[LiveOrderRecord]: ...

    async def symbols(self, session_id: str) -> list[str]: ...

    async def last_trade_ts(self, session_id: str, symbol: str) -> datetime | None: ...


# ---------------------------------------------------------------------------
# asyncpg store
# ---------------------------------------------------------------------------


class PgLiveOrderStore:
    """asyncpg-backed live order ledger on the 0009 schema."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool

    async def create(self, order: LiveOrderRecord) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO live_orders
                    (client_order_id, exchange_order_id, order_list_id, session_id,
                     strategy_id, exchange_id, symbol, side, order_type, quantity, price,
                     stop_price, reduce_only, tag, status, actor, leg_client_order_ids,
                     submitted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15,
                        $16, $17, $18)
                """,
                order.client_order_id,
                order.exchange_order_id,
                order.order_list_id,
                order.session_id,
                order.strategy_id,
                order.exchange_id,
                order.symbol,
                order.side,
                order.order_type,
                order.quantity,
                order.price,
                order.stop_price,
                order.reduce_only,
                order.tag,
                order.status.value,
                order.actor,
                list(order.leg_client_order_ids) or None,
                order.submitted_at,
            )

    async def mark_ack(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        order_list_id: str | None = None,
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE live_orders SET exchange_order_id = $2, status = $3,"
                " order_list_id = COALESCE($4, order_list_id), error = NULL,"
                " terminal_at = CASE WHEN $3 ::text IN"
                "   ('filled', 'canceled', 'expired', 'rejected') THEN now() ELSE NULL END,"
                " updated_at = now()"
                " WHERE client_order_id = $1 AND status IN ('pending_submit', 'submitting')"
                " RETURNING client_order_id",
                client_order_id,
                exchange_order_id,
                status.value,
                order_list_id,
            )
        return row is not None

    async def mark_submit_failed(self, client_order_id: str, error: str) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE live_orders SET status = 'rejected', error = $2, terminal_at = now(),"
                " updated_at = now()"
                " WHERE client_order_id = $1 AND status IN ('pending_submit', 'submitting')"
                " RETURNING client_order_id",
                client_order_id,
                error[:1000],
            )
        return row is not None

    async def resolve_unknown(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        order_list_id: str | None = None,
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE live_orders SET exchange_order_id = $2, status = $3, error = NULL,"
                " order_list_id = COALESCE($6, order_list_id),"
                " filled_quantity = GREATEST(COALESCE($4, 0), COALESCE(filled_quantity, 0)),"
                " average_fill_price = COALESCE($5, average_fill_price),"
                " terminal_at = CASE WHEN $3 ::text IN"
                "   ('filled', 'canceled', 'expired', 'rejected') THEN now() ELSE NULL END,"
                " updated_at = now()"
                " WHERE client_order_id = $1"
                " AND status IN ('pending_submit', 'submitting', 'unknown')"
                " RETURNING client_order_id",
                client_order_id,
                exchange_order_id,
                status.value,
                filled_quantity,
                average_fill_price,
                order_list_id,
            )
        return row is not None

    async def apply_status(
        self,
        client_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        error: str | None = None,
    ) -> str:
        # one conditional UPDATE: the WHERE clause is the monotonic plan (F7)
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                UPDATE live_orders SET
                    status = $2,
                    filled_quantity = COALESCE($3 ::numeric, COALESCE(filled_quantity, 0)),
                    average_fill_price = COALESCE($4 ::numeric, average_fill_price),
                    error = $5 ::text,
                    terminal_at = CASE WHEN $2 ::text IN
                      ('filled', 'canceled', 'expired', 'rejected') THEN now() ELSE NULL END,
                    updated_at = now()
                WHERE client_order_id = $1
                  AND status NOT IN ('filled', 'canceled', 'expired', 'rejected')
                  AND ($3 ::numeric IS NULL OR $3 ::numeric >= COALESCE(filled_quantity, 0))
                  AND NOT (status = $2
                           AND ($3 ::numeric IS NULL
                                OR $3 ::numeric = COALESCE(filled_quantity, 0))
                           AND $5 ::text IS NULL)
                RETURNING client_order_id
                """,
                client_order_id,
                status.value,
                filled_quantity,
                average_fill_price,
                error,
            )
            if row is not None:
                return APPLIED
            row = await conn.fetchrow(
                "SELECT status, filled_quantity FROM live_orders WHERE client_order_id = $1",
                client_order_id,
            )
        if row is None:
            return REFUSED
        regressing = (
            filled_quantity is not None
            and row["filled_quantity"] is not None
            and (filled_quantity < row["filled_quantity"])
        )
        if str(row["status"]) in {s.value for s in TERMINAL_STATUSES} or regressing:
            return REFUSED
        return UNCHANGED

    async def record_fill(self, fill: LiveFillRecord) -> bool:
        """The claim (F2): the row lands unapplied; the sweep applies then confirms."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO live_fills
                    (trade_id, client_order_id, session_id, exchange_id, symbol, side,
                     price, quantity, fee, fee_currency, liquidity, timestamp, applied)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, FALSE)
                ON CONFLICT (exchange_id, symbol, trade_id) DO NOTHING
                RETURNING id
                """,
                fill.trade_id,
                fill.client_order_id,
                fill.session_id,
                fill.exchange_id,
                fill.symbol,
                fill.side,
                fill.price,
                fill.quantity,
                fill.fee,
                fill.fee_currency,
                fill.liquidity,
                fill.timestamp,
            )
        return row is not None

    async def unapplied_fills(self, session_id: str) -> list[LiveFillRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM live_fills WHERE session_id = $1 AND NOT applied ORDER BY id",
                session_id,
            )
        return [_row_to_fill(r) for r in rows]

    async def mark_fill_applied(self, fill: LiveFillRecord) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE live_fills SET applied = TRUE"
                " WHERE exchange_id = $1 AND symbol = $2 AND trade_id = $3 AND NOT applied"
                " RETURNING id",
                fill.exchange_id,
                fill.symbol,
                fill.trade_id,
            )
        return row is not None

    async def open_orders(self, session_id: str) -> list[LiveOrderRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM live_orders WHERE session_id = $1"
                " AND status NOT IN ('filled', 'canceled', 'expired', 'rejected')"
                " ORDER BY COALESCE(submitted_at, updated_at), id",
                session_id,
            )
        return [_row_to_order(r) for r in rows]

    async def orders(self, session_id: str, limit: int = 100) -> list[LiveOrderRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM live_orders WHERE session_id = $1"
                " ORDER BY COALESCE(submitted_at, updated_at) DESC, id DESC LIMIT $2",
                session_id,
                limit,
            )
        return [_row_to_order(r) for r in rows]

    async def by_client_order_id(self, client_order_id: str) -> LiveOrderRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM live_orders WHERE client_order_id = $1", client_order_id
            )
        return None if row is None else _row_to_order(row)

    async def by_exchange_order_id(
        self, exchange_order_id: str, session_id: str
    ) -> LiveOrderRecord | None:
        # F1 + F12: session-scoped, and a list id never matches an order row
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM live_orders WHERE exchange_order_id = $1 AND session_id = $2"
                " AND order_list_id IS NULL ORDER BY id DESC LIMIT 1",
                exchange_order_id,
                session_id,
            )
        return None if row is None else _row_to_order(row)

    async def by_order_list_id(self, order_list_id: str, session_id: str) -> LiveOrderRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM live_orders WHERE order_list_id = $1 AND session_id = $2"
                " ORDER BY id DESC LIMIT 1",
                order_list_id,
                session_id,
            )
        return None if row is None else _row_to_order(row)

    async def tracked_by_exchange_order_id(self, exchange_order_id: str) -> bool:
        # A1 (R29): account-wide — the untracked classification, never fill attribution
        async with self._pool.acquire() as conn:
            found = await conn.fetchval(
                "SELECT 1 FROM live_orders WHERE exchange_order_id = $1"
                " AND order_list_id IS NULL LIMIT 1",
                exchange_order_id,
            )
        return found is not None

    async def tracked_by_order_list_id(self, order_list_id: str) -> bool:
        async with self._pool.acquire() as conn:
            found = await conn.fetchval(
                "SELECT 1 FROM live_orders WHERE order_list_id = $1 LIMIT 1", order_list_id
            )
        return found is not None

    async def unsettled(self, session_id: str, older_than: datetime) -> list[LiveOrderRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM live_orders WHERE session_id = $1"
                " AND status IN ('pending_submit', 'submitting', 'unknown')"
                " AND COALESCE(submitted_at, updated_at) < $2 ORDER BY id",
                session_id,
                older_than,
            )
        return [_row_to_order(r) for r in rows]

    async def refreshable(self, session_id: str) -> list[LiveOrderRecord]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM live_orders WHERE session_id = $1"
                " AND status IN ('new', 'partially_filled') AND exchange_order_id IS NOT NULL"
                " ORDER BY id",
                session_id,
            )
        return [_row_to_order(r) for r in rows]

    async def symbols(self, session_id: str) -> list[str]:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT DISTINCT symbol FROM live_orders WHERE session_id = $1 ORDER BY symbol",
                session_id,
            )
        return [str(r["symbol"]) for r in rows]

    async def last_trade_ts(self, session_id: str, symbol: str) -> datetime | None:
        async with self._pool.acquire() as conn:
            value = await conn.fetchval(
                "SELECT MAX(timestamp) FROM live_fills WHERE session_id = $1 AND symbol = $2",
                session_id,
                symbol,
            )
        return _ts(value)


def _row_to_order(row: Any) -> LiveOrderRecord:
    raw = dict(row)
    try:
        status = LiveOrderStatus(str(raw["status"]))
    except ValueError:
        status = LiveOrderStatus.UNKNOWN
    return LiveOrderRecord(
        client_order_id=str(raw["client_order_id"]),
        session_id=str(raw["session_id"]),
        strategy_id=str(raw["strategy_id"]),
        exchange_id=str(raw["exchange_id"]),
        symbol=str(raw["symbol"]),
        side=str(raw["side"]),
        order_type=str(raw["order_type"]),
        quantity=_dec(raw["quantity"]),
        price=_dec_opt(raw["price"]),
        stop_price=_dec_opt(raw["stop_price"]),
        filled_quantity=_dec(raw.get("filled_quantity") or 0),
        average_fill_price=_dec_opt(raw.get("average_fill_price")),
        fee=_dec_opt(raw.get("fee")),
        fee_currency=raw.get("fee_currency"),
        reduce_only=bool(raw.get("reduce_only")),
        tag=str(raw.get("tag") or ""),
        actor=str(raw.get("actor") or ""),
        status=status,
        exchange_order_id=(
            None if raw.get("exchange_order_id") is None else str(raw["exchange_order_id"])
        ),
        order_list_id=None if raw.get("order_list_id") is None else str(raw["order_list_id"]),
        leg_client_order_ids=tuple(str(c) for c in raw.get("leg_client_order_ids") or ()),
        submitted_at=_ts(raw.get("submitted_at")),
        terminal_at=_ts(raw.get("terminal_at")),
        updated_at=_ts(raw.get("updated_at")),
        error=raw.get("error"),
    )


def _row_to_fill(row: Any) -> LiveFillRecord:
    raw = dict(row)
    return LiveFillRecord(
        trade_id=str(raw["trade_id"]),
        client_order_id=str(raw["client_order_id"]),
        session_id=str(raw["session_id"]),
        exchange_id=str(raw["exchange_id"]),
        symbol=str(raw["symbol"]),
        side=str(raw["side"]),
        price=_dec(raw["price"]),
        quantity=_dec(raw["quantity"]),
        fee=_dec(raw["fee"]),
        fee_currency=str(raw.get("fee_currency") or ""),
        liquidity=str(raw.get("liquidity") or ""),
        timestamp=cast("datetime", raw["timestamp"]),  # NOT NULL in the schema
    )


# ---------------------------------------------------------------------------
# In-memory twin
# ---------------------------------------------------------------------------


class InMemoryLiveOrderStore:
    """The unit-test twin: the same contract without a database."""

    def __init__(self) -> None:
        self._orders: dict[str, LiveOrderRecord] = {}
        self._by_exchange: dict[str, str] = {}
        self._by_list: dict[str, str] = {}
        self._fills: dict[tuple[str, str, str], LiveFillRecord] = {}
        self._applied: set[tuple[str, str, str]] = set()

    def _index(self, order: LiveOrderRecord) -> None:
        if order.order_list_id:
            self._by_list[order.order_list_id] = order.client_order_id
        elif order.exchange_order_id:
            self._by_exchange[order.exchange_order_id] = order.client_order_id

    async def create(self, order: LiveOrderRecord) -> None:
        if order.client_order_id in self._orders:
            raise ValueError(f"duplicate client_order_id {order.client_order_id}")
        self._orders[order.client_order_id] = order
        self._index(order)

    async def mark_ack(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        order_list_id: str | None = None,
    ) -> bool:
        order = self._orders.get(client_order_id)
        if order is None:
            return False
        if order.status not in _ACKABLE:
            _refused(order, "ack")
            return False
        self._orders[client_order_id] = replace(
            order,
            status=status,
            exchange_order_id=exchange_order_id,
            order_list_id=order_list_id or order.order_list_id,
            error=None,
            terminal_at=_terminal_now(status),
            updated_at=datetime.now(UTC),
        )
        self._index(self._orders[client_order_id])
        return True

    async def mark_submit_failed(self, client_order_id: str, error: str) -> bool:
        order = self._orders.get(client_order_id)
        if order is None:
            return False
        if order.status not in _ACKABLE:
            _refused(order, "submit-failed mark")
            return False
        self._orders[client_order_id] = replace(
            order,
            status=LiveOrderStatus.REJECTED,
            error=error,
            terminal_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
        return True

    async def resolve_unknown(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        order_list_id: str | None = None,
    ) -> bool:
        order = self._orders.get(client_order_id)
        if order is None or order.status not in UNSETTLED_STATUSES:
            if order is not None:
                _refused(order, "resolve")
            return False
        quantity = (
            max(order.filled_quantity, filled_quantity)
            if filled_quantity is not None
            else order.filled_quantity
        )
        average = average_fill_price if average_fill_price is not None else order.average_fill_price
        self._orders[client_order_id] = replace(
            order,
            status=status,
            exchange_order_id=exchange_order_id,
            order_list_id=order_list_id or order.order_list_id,
            filled_quantity=quantity,
            average_fill_price=average,
            error=None,
            terminal_at=_terminal_now(status),
            updated_at=datetime.now(UTC),
        )
        self._index(self._orders[client_order_id])
        return True

    async def apply_status(
        self,
        client_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
        error: str | None = None,
    ) -> str:
        order = self._orders.get(client_order_id)
        if order is None:
            return REFUSED
        updated, outcome = _plan_status(order, status, filled_quantity, average_fill_price, error)
        if outcome == APPLIED and updated is not None:
            self._orders[client_order_id] = updated
            self._index(updated)
        return outcome

    async def record_fill(self, fill: LiveFillRecord) -> bool:
        key = (fill.exchange_id, fill.symbol, fill.trade_id)
        if key in self._fills:
            return False
        self._fills[key] = fill
        return True

    async def unapplied_fills(self, session_id: str) -> list[LiveFillRecord]:
        return [
            fill
            for key, fill in self._fills.items()
            if fill.session_id == session_id and key not in self._applied
        ]

    async def mark_fill_applied(self, fill: LiveFillRecord) -> bool:
        key = (fill.exchange_id, fill.symbol, fill.trade_id)
        if key not in self._fills or key in self._applied:
            return False
        self._applied.add(key)
        return True

    def _session(self, session_id: str) -> list[LiveOrderRecord]:
        return [o for o in self._orders.values() if o.session_id == session_id]

    async def open_orders(self, session_id: str) -> list[LiveOrderRecord]:
        return [o for o in self._session(session_id) if o.status not in TERMINAL_STATUSES]

    async def orders(self, session_id: str, limit: int = 100) -> list[LiveOrderRecord]:
        rows = self._session(session_id)
        rows.sort(key=lambda o: o.submitted_at or o.updated_at or _EPOCH, reverse=True)
        return rows[:limit]

    async def by_client_order_id(self, client_order_id: str) -> LiveOrderRecord | None:
        return self._orders.get(client_order_id)

    async def by_exchange_order_id(
        self, exchange_order_id: str, session_id: str
    ) -> LiveOrderRecord | None:
        cid = self._by_exchange.get(exchange_order_id)
        order = None if cid is None else self._orders.get(cid)
        if order is None or order.session_id != session_id:
            return None
        return order

    async def by_order_list_id(self, order_list_id: str, session_id: str) -> LiveOrderRecord | None:
        cid = self._by_list.get(order_list_id)
        order = None if cid is None else self._orders.get(cid)
        if order is None or order.session_id != session_id:
            return None
        return order

    async def tracked_by_exchange_order_id(self, exchange_order_id: str) -> bool:
        return any(
            o.exchange_order_id == exchange_order_id and o.order_list_id is None
            for o in self._orders.values()
        )

    async def tracked_by_order_list_id(self, order_list_id: str) -> bool:
        return any(o.order_list_id == order_list_id for o in self._orders.values())

    async def unsettled(self, session_id: str, older_than: datetime) -> list[LiveOrderRecord]:
        return [
            o
            for o in self._session(session_id)
            if o.status in UNSETTLED_STATUSES
            and (o.submitted_at or o.updated_at or _EPOCH) < older_than
        ]

    async def refreshable(self, session_id: str) -> list[LiveOrderRecord]:
        return [
            o
            for o in self._session(session_id)
            if o.status in (LiveOrderStatus.NEW, LiveOrderStatus.PARTIALLY_FILLED)
            and o.exchange_order_id is not None
        ]

    async def symbols(self, session_id: str) -> list[str]:
        return sorted({o.symbol for o in self._session(session_id)})

    async def last_trade_ts(self, session_id: str, symbol: str) -> datetime | None:
        stamps = [
            f.timestamp
            for f in self._fills.values()
            if f.session_id == session_id and f.symbol == symbol
        ]
        return max(stamps) if stamps else None


_EPOCH = datetime(1, 1, 1, tzinfo=UTC)
