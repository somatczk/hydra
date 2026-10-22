"""SQL persistence for trading_sessions rows (the kill switch lives in RiskService's store)."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hydra.execution.session_manager import TradingSession

logger = logging.getLogger(__name__)


async def persist_session(db_pool: Any, session: TradingSession, *, attempts: int = 1) -> bool:
    """Upsert the session row; False when every attempt failed (each failure is logged)."""
    if db_pool is None:
        return True
    for attempt in range(1, attempts + 1):
        try:
            await _upsert(db_pool, session)
            return True
        except Exception:
            logger.exception(
                "Failed to persist session %s (attempt %d/%d)",
                session.session_id,
                attempt,
                attempts,
            )
        if attempt < attempts:
            await asyncio.sleep(0.2 * attempt)
    return False


async def _upsert(db_pool: Any, session: TradingSession) -> None:
    async with db_pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO trading_sessions
                (id, strategy_id, trading_mode, status, exchange_id,
                 symbols, timeframe, paper_capital, started_at,
                 stopped_at, error_message, positions_at_stop, strategy_config, config_hash)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13::jsonb, $14)
            ON CONFLICT (id) DO UPDATE SET
                status = EXCLUDED.status,
                started_at = EXCLUDED.started_at,
                stopped_at = EXCLUDED.stopped_at,
                error_message = EXCLUDED.error_message,
                positions_at_stop = EXCLUDED.positions_at_stop,
                strategy_config = COALESCE(
                    EXCLUDED.strategy_config, trading_sessions.strategy_config),
                config_hash = COALESCE(EXCLUDED.config_hash, trading_sessions.config_hash)
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
            json.dumps(session.strategy_config) if session.strategy_config is not None else None,
            session.config_hash,
        )


async def load_session_trades(db_pool: Any, session: TradingSession) -> list[dict[str, Any]]:
    """Load trades from DB for a stopped session."""
    if db_pool is None:
        return []
    try:
        source = session.trading_mode  # 'paper' or 'live'
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, symbol, side, quantity, price, fee, pnl, net_pnl, reason, timestamp
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
                    "net_pnl": float(row["net_pnl"]) if row["net_pnl"] is not None else None,
                    "reason": row["reason"],
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
            "SELECT * FROM trading_sessions WHERE status = 'running' ORDER BY created_at"
        )
    return rows


async def load_recent(db_pool: Any) -> list[Any]:
    """Return the 50 most recently created session rows."""
    async with db_pool.acquire() as conn:
        rows: list[Any] = await conn.fetch(
            "SELECT * FROM trading_sessions ORDER BY created_at DESC LIMIT 50"
        )
    return rows


async def running_ids(db_pool: Any) -> set[str]:
    """Ids of the rows the database says should be running (health snapshot)."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id FROM trading_sessions WHERE status = 'running'")
    return {str(r["id"]) for r in rows}
