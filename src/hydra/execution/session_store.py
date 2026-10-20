"""SQL persistence for trading sessions and the global kill-switch flag."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hydra.execution.session_manager import TradingSession

logger = logging.getLogger(__name__)


async def read_kill_switch(db_pool: Any) -> Any:
    """Return the global ``kill_switch_active`` value."""
    async with db_pool.acquire() as conn:
        return await conn.fetchval(
            "SELECT kill_switch_active FROM risk_config WHERE scope = 'global'"
        )


async def set_kill_switch(db_pool: Any, active: bool) -> None:
    """Set or clear the global kill-switch flag."""
    async with db_pool.acquire() as conn:
        if active:
            await conn.execute(
                "UPDATE risk_config SET kill_switch_active = TRUE, "
                "updated_at = now() WHERE scope = 'global'"
            )
        else:
            await conn.execute(
                "UPDATE risk_config SET kill_switch_active = FALSE, "
                "updated_at = now() WHERE scope = 'global'"
            )


async def persist_session(db_pool: Any, session: TradingSession) -> None:
    """Upsert session row to DB."""
    if db_pool is None:
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO trading_sessions
                    (id, strategy_id, trading_mode, status, exchange_id,
                     symbols, timeframe, paper_capital, started_at,
                     stopped_at, error_message, positions_at_stop)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
                ON CONFLICT (id) DO UPDATE SET
                    status = EXCLUDED.status,
                    started_at = EXCLUDED.started_at,
                    stopped_at = EXCLUDED.stopped_at,
                    error_message = EXCLUDED.error_message,
                    positions_at_stop = EXCLUDED.positions_at_stop
                """,
                session.session_id,
                session.strategy_id,
                session.trading_mode,
                session.status,
                session.exchange_id,
                session.symbols,
                session.timeframe,
                session.paper_capital,
                session.started_at,
                session.stopped_at,
                session.error_message,
                session.positions_at_stop,
            )
    except Exception:
        logger.exception("Failed to persist session %s", session.session_id)


async def load_session_trades(db_pool: Any, session: TradingSession) -> list[dict[str, Any]]:
    """Load trades from DB for a stopped session."""
    if db_pool is None:
        return []
    try:
        source = session.trading_mode  # 'paper' or 'live'
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, symbol, side, quantity, price, fee, pnl, timestamp
                FROM trades
                WHERE session_id = $1 AND source = $2
                ORDER BY timestamp ASC
                """,
                session.session_id,
                source,
            )
            return [
                {
                    "id": str(row["id"]),
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "quantity": float(row["quantity"]) if row["quantity"] else 0,
                    "price": float(row["price"]) if row["price"] else 0,
                    "fee": float(row["fee"]) if row["fee"] else 0,
                    "pnl": float(row["pnl"]) if row["pnl"] else 0,
                    "timestamp": row["timestamp"].isoformat() if row["timestamp"] else None,
                }
                for row in rows
            ]
    except Exception:
        logger.exception("Failed to load trades for session %s", session.session_id)
        return []


async def load_running(db_pool: Any) -> list[Any]:
    """Return every session row whose status is 'running'."""
    async with db_pool.acquire() as conn:
        rows: list[Any] = await conn.fetch(
            "SELECT * FROM trading_sessions WHERE status = 'running'"
        )
    return rows


async def load_recent(db_pool: Any) -> list[Any]:
    """Return the 50 most recently created session rows."""
    async with db_pool.acquire() as conn:
        rows: list[Any] = await conn.fetch(
            "SELECT * FROM trading_sessions ORDER BY created_at DESC LIMIT 50"
        )
    return rows


async def mark_recovery_failed(db_pool: Any, session_id: str) -> None:
    """Mark a session row as errored after a failed recovery."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE trading_sessions SET status = 'error', "
            "error_message = 'Recovery failed' WHERE id = $1",
            session_id,
        )


async def fetch_exchange_credentials(db_pool: Any, exchange_id: str) -> Any:
    """Return the encrypted credential row for *exchange_id*, or None."""
    async with db_pool.acquire() as conn:
        return await conn.fetchrow(
            "SELECT encrypted_key, encrypted_secret, "
            "encrypted_passphrase "
            "FROM exchange_credentials "
            "WHERE exchange_id = $1",
            exchange_id,
        )
