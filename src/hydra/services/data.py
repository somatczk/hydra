"""Stored market-data reads: the bar coverage behind the ``data_coverage`` tool."""

from __future__ import annotations

import logging
from typing import Any

from hydra.core.types import Timeframe
from hydra.data.storage import MarketDataRepository
from hydra.services.errors import db_errors, require_pool, service_error

logger = logging.getLogger(__name__)


async def bar_coverage(
    pool: Any, exchange_id: str, symbol: str, timeframe: Timeframe
) -> dict[str, Any]:
    """The stored range for one market: ``first_ts``/``last_ts`` (ISO, UTC) and ``bar_count``.

    404 ``no_stored_data`` when nothing is stored for the exchange/symbol/timeframe;
    503 when the process has no pool or the database cannot answer.
    """
    pool = require_pool(pool)
    repo = MarketDataRepository.from_pool(pool)
    async with db_errors("reading stored bar coverage"):
        stored = await repo.get_bar_range(exchange_id, symbol, timeframe)
    if stored is None:
        raise service_error(
            404,
            "no_stored_data",
            f"No stored bars for {exchange_id} {symbol} {timeframe}",
            "no_data",
            exchange_id=exchange_id,
            symbol=symbol,
            timeframe=str(timeframe),
        )
    first, last, count = stored
    return {
        "exchange_id": exchange_id,
        "symbol": symbol,
        "timeframe": str(timeframe),
        "first_ts": first.isoformat(),
        "last_ts": last.isoformat(),
        "bar_count": count,
    }
