"""Risk-related market data: volume averages and correlation matrices.

Fetches OHLCV data from the exchange to compute average daily volume
and pairwise correlation for risk checks.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

MIN_CORRELATION_RETURNS = 20
"""Daily log returns a pair needs before its correlation counts (the 30-day fetch gives 29)."""


@dataclass
class RiskMarketData:
    """Cached risk market data for pre-trade checks."""

    average_volume: dict[str, Decimal] = field(default_factory=dict)
    correlation_map: dict[str, list[str]] = field(default_factory=dict)


async def fetch_risk_market_data(
    exchange_client: Any,
    symbols: list[str],
    correlation_threshold: float = 0.70,
) -> RiskMarketData:
    """Fetch 30-day daily OHLCV and compute volume + correlation.

    Parameters
    ----------
    exchange_client:
        A CCXT exchange instance (or ExchangeClient with a ``_get_exchange()`` method).
    symbols:
        List of trading symbols to fetch data for.
    correlation_threshold:
        Pearson correlation of daily log returns above which two symbols are considered
        highly correlated.
    """
    result = RiskMarketData()

    if not symbols:
        return result

    exchange = exchange_client
    if hasattr(exchange_client, "_get_exchange"):
        exchange = exchange_client._get_exchange()

    close_series: dict[str, list[float]] = {}

    for symbol in symbols:
        try:
            ohlcv = await exchange.fetch_ohlcv(symbol, "1d", limit=30)
            if not ohlcv:
                continue

            volumes = [candle[5] for candle in ohlcv if candle[5] is not None]
            closes = [candle[4] for candle in ohlcv if candle[4] is not None]

            if volumes:
                avg_vol = sum(volumes) / len(volumes)
                result.average_volume[symbol] = Decimal(str(round(avg_vol, 2)))

            if closes and min(closes) > 0:
                close_series[symbol] = closes

        except Exception:
            logger.warning("Failed to fetch OHLCV for %s", symbol, exc_info=True)

    # Correlate daily returns: the price levels of any two trending coins correlate.
    if len(close_series) >= 2:
        syms = list(close_series.keys())
        min_len = min(len(v) for v in close_series.values())
        if min_len - 1 >= MIN_CORRELATION_RETURNS:
            matrix = np.array([close_series[s][-min_len:] for s in syms], dtype=np.float64)
            with np.errstate(divide="ignore", invalid="ignore"):
                corr = np.corrcoef(np.diff(np.log(matrix), axis=1))

            for i, sym_i in enumerate(syms):
                correlated = []
                for j, sym_j in enumerate(syms):
                    if i != j and abs(corr[i, j]) >= correlation_threshold:
                        correlated.append(sym_j)
                if correlated:
                    result.correlation_map[sym_i] = correlated

    return result
