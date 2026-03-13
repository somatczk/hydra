"""M03: Technical indicator library (TA-Lib + custom crypto)."""

from __future__ import annotations

from hydra.indicators.custom import (
    funding_rate_sma,
    liquidation_intensity,
    taker_buy_ratio,
)
from hydra.indicators.library import (
    atr,
    bollinger_bands,
    cci,
    ema,
    ichimoku,
    keltner_channels,
    macd,
    mfi,
    obv,
    rsi,
    sma,
    stochastic,
    supertrend,
    vwap,
    williams_r,
)

__all__ = [
    # Volatility
    "atr",
    "bollinger_bands",
    "cci",
    "ema",
    # Crypto-specific
    "funding_rate_sma",
    "ichimoku",
    "keltner_channels",
    "liquidation_intensity",
    "macd",
    "mfi",
    # Volume
    "obv",
    # Momentum
    "rsi",
    # Trend
    "sma",
    "stochastic",
    "supertrend",
    "taker_buy_ratio",
    "vwap",
    "williams_r",
]
