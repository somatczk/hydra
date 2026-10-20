"""Load and save paper-account state (schema: Alembic revision 0002_paper_state).

``save`` writes one transaction: the account's full JSONB state, its unpersisted trade rows
(idempotent on ``(session_id, fill_seq)``), the per-symbol ``positions`` rows and, when given,
one ``balance_snapshots`` row. Decimals travel as strings inside the JSONB so nothing is rounded.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


class StaleStateError(RuntimeError):
    """The stored state_version is not older than the one being written (a second writer)."""


@dataclass(frozen=True, slots=True)
class AccountRow:
    session_id: str
    strategy_id: str
    exchange_id: str
    quote_asset: str
    state: dict[str, Any]
    state_version: int


@dataclass(frozen=True, slots=True)
class TradeRow:
    symbol: str
    side: str
    price: Decimal
    quantity: Decimal
    fee: Decimal
    gross_pnl: Decimal
    net_pnl: Decimal | None
    order_id: str
    order_type: str
    fill_seq: int
    liquidity: str
    reason: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class PositionRow:
    symbol: str
    quantity: Decimal
    avg_entry_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    timestamp: datetime
    total_value: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    drawdown_pct: Decimal
    peak_value: Decimal


@dataclass(frozen=True, slots=True)
class StoredAccount:
    session_id: str
    strategy_id: str
    exchange_id: str
    quote_asset: str
    state: dict[str, Any]
    state_version: int


_UPSERT_ACCOUNT = """
    INSERT INTO paper_accounts
        (session_id, strategy_id, exchange_id, quote_asset, state, state_version, updated_at)
    VALUES ($1, $2, $3, $4, $5::jsonb, $6, now())
    ON CONFLICT (session_id) DO UPDATE SET
        state = EXCLUDED.state,
        state_version = EXCLUDED.state_version,
        updated_at = EXCLUDED.updated_at
    WHERE paper_accounts.state_version < EXCLUDED.state_version
"""

_INSERT_TRADE = """
    INSERT INTO trades
        (symbol, side, price, quantity, fee, pnl, net_pnl, strategy_id, exchange_id,
         timestamp, source, session_id, order_id, fill_seq, liquidity, reason, order_type)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, 'paper', $11, $12, $13, $14, $15, $16)
    ON CONFLICT (session_id, fill_seq) WHERE fill_seq IS NOT NULL DO NOTHING
"""

_UPSERT_POSITION = """
    INSERT INTO positions
        (strategy_id, session_id, exchange_id, symbol, direction, quantity, avg_entry_price,
         unrealized_pnl, realized_pnl, source, updated_at)
    VALUES ($1, $2, $3, $4, 'LONG', $5, $6, $7, $8, 'paper', now())
    ON CONFLICT (session_id, symbol) DO UPDATE SET
        direction = EXCLUDED.direction,
        quantity = EXCLUDED.quantity,
        avg_entry_price = EXCLUDED.avg_entry_price,
        unrealized_pnl = EXCLUDED.unrealized_pnl,
        realized_pnl = EXCLUDED.realized_pnl,
        updated_at = EXCLUDED.updated_at
"""

_INSERT_SNAPSHOT = """
    INSERT INTO balance_snapshots
        (timestamp, total_value, unrealized_pnl, realized_pnl, drawdown_pct, peak_value,
         source, session_id)
    VALUES ($1, $2, $3, $4, $5, $6, 'paper', $7)
"""


def dumps_state(state: dict[str, Any]) -> str:
    """JSON text of a state dict whose numbers are already strings; rejects anything else."""
    return json.dumps(state, sort_keys=True, allow_nan=False)


def loads_state(raw: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    loaded = json.loads(raw)
    if not isinstance(loaded, dict):
        raise ValueError("paper_accounts.state is not a JSON object")
    return loaded


async def save(
    conn: Any,
    account: AccountRow,
    trades: Sequence[TradeRow] = (),
    positions: Sequence[PositionRow] = (),
    flat_symbols: Sequence[str] = (),
    snapshot: SnapshotRow | None = None,
) -> None:
    """Write the account, its new trades, positions and an optional snapshot atomically."""
    async with conn.transaction():
        status = await conn.execute(
            _UPSERT_ACCOUNT,
            account.session_id,
            account.strategy_id,
            account.exchange_id,
            account.quote_asset,
            dumps_state(account.state),
            account.state_version,
        )
        if not str(status).endswith(" 1"):
            raise StaleStateError(
                f"paper_accounts row for {account.session_id} has state_version >= "
                f"{account.state_version}"
            )
        for t in trades:
            await conn.execute(
                _INSERT_TRADE,
                t.symbol,
                t.side,
                t.price,
                t.quantity,
                t.fee,
                t.gross_pnl,
                t.net_pnl,
                account.strategy_id,
                account.exchange_id,
                t.timestamp,
                account.session_id,
                t.order_id,
                t.fill_seq,
                t.liquidity,
                t.reason,
                t.order_type,
            )
        for p in positions:
            await conn.execute(
                _UPSERT_POSITION,
                account.strategy_id,
                account.session_id,
                account.exchange_id,
                p.symbol,
                p.quantity,
                p.avg_entry_price,
                p.unrealized_pnl,
                p.realized_pnl,
            )
        for symbol in flat_symbols:
            await conn.execute(
                "DELETE FROM positions WHERE session_id = $1 AND symbol = $2",
                account.session_id,
                symbol,
            )
        if snapshot is not None:
            await conn.execute(
                _INSERT_SNAPSHOT,
                snapshot.timestamp,
                snapshot.total_value,
                snapshot.unrealized_pnl,
                snapshot.realized_pnl,
                snapshot.drawdown_pct,
                snapshot.peak_value,
                account.session_id,
            )


async def load(conn: Any, session_id: str) -> StoredAccount | None:
    row = await conn.fetchrow(
        "SELECT session_id, strategy_id, exchange_id, quote_asset, state, state_version "
        "FROM paper_accounts WHERE session_id = $1",
        session_id,
    )
    if row is None:
        return None
    return StoredAccount(
        session_id=str(row["session_id"]),
        strategy_id=str(row["strategy_id"]),
        exchange_id=str(row["exchange_id"]),
        quote_asset=str(row["quote_asset"]),
        state=loads_state(row["state"]),
        state_version=int(row["state_version"]),
    )


async def has_open_positions(conn: Any, session_id: str) -> bool:
    """True when the positions table holds a non-zero row for the session."""
    found = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM positions WHERE session_id = $1 AND quantity <> 0)",
        session_id,
    )
    return bool(found)


async def load_trades(conn: Any, session_id: str) -> list[TradeRow]:
    """The session's paper trade rows in ``fill_seq`` order."""
    rows = await conn.fetch(
        "SELECT symbol, side, price, quantity, fee, pnl, net_pnl, order_id, order_type, fill_seq, "
        "liquidity, reason, timestamp FROM trades "
        "WHERE session_id = $1 AND fill_seq IS NOT NULL ORDER BY fill_seq",
        session_id,
    )
    return [
        TradeRow(
            symbol=str(r["symbol"]),
            side=str(r["side"]),
            price=Decimal(r["price"]),
            quantity=Decimal(r["quantity"]),
            fee=Decimal(r["fee"]),
            gross_pnl=Decimal(r["pnl"]),
            net_pnl=None if r["net_pnl"] is None else Decimal(r["net_pnl"]),
            order_id=str(r["order_id"]),
            order_type=str(r["order_type"] or ""),
            fill_seq=int(r["fill_seq"]),
            liquidity=str(r["liquidity"]),
            reason=str(r["reason"] or ""),
            timestamp=r["timestamp"],
        )
        for r in rows
    ]
