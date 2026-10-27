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
  decreases, and a same-status, same-quantity re-apply is the no-op ``unchanged``.
* ``record_fill`` — the double-apply guard: UNIQUE (exchange_id, symbol, trade_id) at the DB
  (ON CONFLICT DO NOTHING) and set membership in memory; True only when newly recorded.

``client_order_id`` is hydra's own ``newClientOrderId`` on the wire: ``hyd-`` + a uuid4 hex
(36 characters, Binance's limit). The prefix is the ownership marker live adopt gates on
(R5: the user's orders are never hijacked).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Protocol

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
    """One live order row; ``exchange_order_id`` is the orderListId for an OCO."""

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
        self, client_order_id: str, exchange_order_id: str, status: LiveOrderStatus
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

    async def open_orders(self, session_id: str) -> list[LiveOrderRecord]: ...

    async def orders(self, session_id: str, limit: int = 100) -> list[LiveOrderRecord]: ...

    async def by_client_order_id(self, client_order_id: str) -> LiveOrderRecord | None: ...

    async def by_exchange_order_id(self, exchange_order_id: str) -> LiveOrderRecord | None: ...

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
                    (client_order_id, exchange_order_id, session_id, strategy_id, exchange_id,
                     symbol, side, order_type, quantity, price, stop_price, reduce_only, tag,
                     status, actor, submitted_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16)
                """,
                order.client_order_id,
                order.exchange_order_id,
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
                order.submitted_at,
            )

    async def mark_ack(
        self, client_order_id: str, exchange_order_id: str, status: LiveOrderStatus
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE live_orders SET exchange_order_id = $2, status = $3, error = NULL,"
                " terminal_at = CASE WHEN $3 ::text IN"
                "   ('filled', 'canceled', 'expired', 'rejected') THEN now() ELSE NULL END,"
                " updated_at = now()"
                " WHERE client_order_id = $1 AND status IN ('pending_submit', 'submitting')"
                " RETURNING client_order_id",
                client_order_id,
                exchange_order_id,
                status.value,
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
    ) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "UPDATE live_orders SET exchange_order_id = $2, status = $3, error = NULL,"
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
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM live_orders WHERE client_order_id = $1", client_order_id
            )
            if row is None:
                return REFUSED
            updated, outcome = _plan_status(
                _row_to_order(row), status, filled_quantity, average_fill_price, error
            )
            if outcome != APPLIED or updated is None:
                return outcome
            await conn.execute(
                "UPDATE live_orders SET status = $2, filled_quantity = $3,"
                " average_fill_price = $4, error = $5,"
                " terminal_at = CASE WHEN $2 ::text IN"
                "   ('filled', 'canceled', 'expired', 'rejected') THEN now() ELSE NULL END,"
                " updated_at = now() WHERE client_order_id = $1",
                client_order_id,
                status.value,
                updated.filled_quantity,
                updated.average_fill_price,
                error,
            )
            return APPLIED

    async def record_fill(self, fill: LiveFillRecord) -> bool:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO live_fills
                    (trade_id, client_order_id, session_id, exchange_id, symbol, side,
                     price, quantity, fee, fee_currency, liquidity, timestamp)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
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

    async def by_exchange_order_id(self, exchange_order_id: str) -> LiveOrderRecord | None:
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM live_orders WHERE exchange_order_id = $1 ORDER BY id DESC LIMIT 1",
                exchange_order_id,
            )
        return None if row is None else _row_to_order(row)

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
        submitted_at=_ts(raw.get("submitted_at")),
        terminal_at=_ts(raw.get("terminal_at")),
        updated_at=_ts(raw.get("updated_at")),
        error=raw.get("error"),
    )


# ---------------------------------------------------------------------------
# In-memory twin
# ---------------------------------------------------------------------------


class InMemoryLiveOrderStore:
    """The unit-test twin: the same contract without a database."""

    def __init__(self) -> None:
        self._orders: dict[str, LiveOrderRecord] = {}
        self._by_exchange: dict[str, str] = {}
        self._fills: dict[tuple[str, str, str], LiveFillRecord] = {}

    async def create(self, order: LiveOrderRecord) -> None:
        if order.client_order_id in self._orders:
            raise ValueError(f"duplicate client_order_id {order.client_order_id}")
        self._orders[order.client_order_id] = order
        if order.exchange_order_id:
            self._by_exchange[order.exchange_order_id] = order.client_order_id

    async def mark_ack(
        self, client_order_id: str, exchange_order_id: str, status: LiveOrderStatus
    ) -> bool:
        order = self._orders.get(client_order_id)
        if order is None:
            return False
        if order.status not in _ACKABLE:
            _refused(order, "ack")
            return False
        self._orders[client_order_id] = replace(
            order, status=status, exchange_order_id=exchange_order_id, error=None
        )
        self._by_exchange[exchange_order_id] = client_order_id
        return True

    async def mark_submit_failed(self, client_order_id: str, error: str) -> bool:
        order = self._orders.get(client_order_id)
        if order is None:
            return False
        if order.status not in _ACKABLE:
            _refused(order, "submit-failed mark")
            return False
        self._orders[client_order_id] = replace(order, status=LiveOrderStatus.REJECTED, error=error)
        return True

    async def resolve_unknown(
        self,
        client_order_id: str,
        exchange_order_id: str,
        status: LiveOrderStatus,
        *,
        filled_quantity: Decimal | None = None,
        average_fill_price: Decimal | None = None,
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
            filled_quantity=quantity,
            average_fill_price=average,
            error=None,
        )
        self._by_exchange[exchange_order_id] = client_order_id
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
            if updated.exchange_order_id:
                self._by_exchange[updated.exchange_order_id] = client_order_id
        return outcome

    async def record_fill(self, fill: LiveFillRecord) -> bool:
        key = (fill.exchange_id, fill.symbol, fill.trade_id)
        if key in self._fills:
            return False
        self._fills[key] = fill
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

    async def by_exchange_order_id(self, exchange_order_id: str) -> LiveOrderRecord | None:
        cid = self._by_exchange.get(exchange_order_id)
        return None if cid is None else self._orders.get(cid)

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
