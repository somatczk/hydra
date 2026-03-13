"""Crypto-specific indicators for the Hydra trading platform.

Functions tailored to cryptocurrency market data such as funding rates,
taker buy/sell ratios, and liquidation intensity.
"""

from __future__ import annotations

import numpy as np
from numpy import ndarray

from hydra.indicators.library import sma


def funding_rate_sma(funding_rates: ndarray, period: int = 24) -> ndarray:
    """Simple Moving Average of funding rates.

    Useful for identifying persistent positive/negative funding (crowd
    positioning) in perpetual futures markets.

    Parameters
    ----------
    funding_rates:
        Array of funding rate values (e.g. 0.0001 = 0.01%).
    period:
        SMA window, default 24 (one day of 8-hour funding intervals
        on most exchanges maps to 3, but 24 is used for a wider window).
    """
    return sma(funding_rates.astype(np.float64), period)


def taker_buy_ratio(buy_volume: ndarray, total_volume: ndarray) -> ndarray:
    """Taker buy/sell ratio.

    Returns the fraction of volume initiated by buyers.  Values above 0.5
    indicate net buying pressure; below 0.5 indicates net selling pressure.

    Parameters
    ----------
    buy_volume:
        Taker buy volume per bar.
    total_volume:
        Total volume per bar.
    """
    buy = buy_volume.astype(np.float64)
    total = total_volume.astype(np.float64)
    result = np.where(total != 0, buy / total, np.nan)
    return result.astype(np.float64)


def liquidation_intensity(
    long_liq: ndarray,
    short_liq: ndarray,
    volume: ndarray,
) -> ndarray:
    """Liquidation intensity indicator.

    Measures the ratio of total liquidations (long + short) to trading
    volume.  Spikes indicate forced selling/buying cascades which often
    precede reversals or accelerations.

    Returns a ratio; higher values mean more liquidations relative to
    normal volume.

    Parameters
    ----------
    long_liq:
        Long liquidation volume per bar.
    short_liq:
        Short liquidation volume per bar.
    volume:
        Total trading volume per bar.
    """
    total_liq = long_liq.astype(np.float64) + short_liq.astype(np.float64)
    vol = volume.astype(np.float64)
    result = np.where(vol != 0, total_liq / vol, np.nan)
    return result.astype(np.float64)
