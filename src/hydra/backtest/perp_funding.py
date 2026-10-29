"""The perp backtest's funding preflight: make ``ts.funding_rates`` cover the range.

P4-C's helper module for the backtest path (``jobs.py`` stays P5's): before a perp
backtest runs, ``prepare_backtest_funding`` tops the stored funding rows up for the
job's window through the P4-B writer (idempotent, venue-paced, only when the on-demand
engine is alive — the kill-switch posture skips the fetch) and then loads what is
actually stored. The executor accrues only from loaded rows; a window with none is the
honest skip recorded on the account, never a fabricated rate.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal
from typing import Any

from hydra.core.logging import get_logger
from hydra.core.types import ExchangeId
from hydra.data.funding import EXCHANGE_ID, ensure_funding_rates
from hydra.execution.perp_trading import FundingRow

logger = get_logger(__name__)

ClientProvider = Callable[[ExchangeId], Any]


async def load_funding_rows(
    pool: Any,
    symbol: str,
    start: datetime,
    end: datetime,
    *,
    exchange: ExchangeId = EXCHANGE_ID,
) -> dict[str, list[FundingRow]]:
    """The settled funding rows stored for ``[start, end]``, per symbol."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT timestamp, rate FROM ts.funding_rates "
            "WHERE exchange = $1 AND symbol = $2 AND timestamp >= $3 AND timestamp <= $4 "
            "ORDER BY timestamp",
            exchange,
            symbol,
            start,
            end,
        )
    return {symbol: [(r["timestamp"], Decimal(r["rate"])) for r in rows]}


async def prepare_backtest_funding(
    pool: Any,
    client_provider: ClientProvider | None,
    symbol: str,
    start: datetime,
    end: datetime,
) -> tuple[dict[str, list[FundingRow]], str]:
    """Top up the stored funding rows for the window, then load them.

    ``client_provider=None`` (auto-fetch off) or a failed top-up keeps today's stored
    rows — the backtest still runs and the executor records the missing windows; the
    returned state says which happened ("ensured" / "fetch_skipped" / "fetch_failed").
    """
    if client_provider is not None:
        try:
            client = await client_provider(EXCHANGE_ID)
        except Exception:
            logger.warning("funding client could not be built; using stored rows", exc_info=True)
            return await load_funding_rows(pool, symbol, start, end), "fetch_failed"
        try:
            await ensure_funding_rates(pool, client, symbol, start=start, end=end)
        except Exception:
            logger.warning("funding top-up failed for %s; using stored rows", symbol, exc_info=True)
            return await load_funding_rows(pool, symbol, start, end), "fetch_failed"
        state = "ensured"
    else:
        state = "fetch_skipped"
    return await load_funding_rows(pool, symbol, start, end), state
