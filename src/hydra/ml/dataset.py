"""The honest training dataset: stored bars in, one reproducible matrix + provenance out.

The :class:`DatasetBuilder` reads 1h/4h/1d bars through an injected
:class:`~hydra.data.storage.MarketDataRepository`, builds the feature matrix and
the sign target at the spec's horizon, and reports exactly what it ran on per the
P0-B provenance discipline: the range, bar count, gap count and feed/auto source
mix per timeframe. The same rows plus the same spec hash always produce a
byte-identical matrix.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from itertools import pairwise
from typing import Any

import numpy as np
from numpy import ndarray

from hydra.core.types import OHLCV, Timeframe
from hydra.data.bars import timeframe_seconds
from hydra.data.errors import NoMarketDataError
from hydra.data.storage import MarketDataRepository
from hydra.ml.features import (
    FeatureMatrix,
    FeatureSpec,
    build_features,
    build_target,
)


@dataclass(frozen=True)
class TimeframeCoverage:
    """What one timeframe actually contributed to a dataset."""

    timeframe: str
    bar_count: int
    first_ts: datetime | None
    last_ts: datetime | None
    gap_count: int
    sources: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "bar_count": self.bar_count,
            "first_ts": self.first_ts.isoformat() if self.first_ts else None,
            "last_ts": self.last_ts.isoformat() if self.last_ts else None,
            "gap_count": self.gap_count,
            "sources": dict(self.sources),
        }


@dataclass(frozen=True)
class DatasetProvenance:
    """Which stored bars a training dataset actually ran on."""

    exchange: str
    symbol: str
    spec_version: int
    horizon: int
    coverage: tuple[TimeframeCoverage, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange": self.exchange,
            "symbol": self.symbol,
            "spec_version": self.spec_version,
            "horizon": self.horizon,
            "coverage": [c.to_dict() for c in self.coverage],
        }


def _gap_count(bars: list[OHLCV], timeframe: Timeframe) -> int:
    """Count consecutive bar pairs further apart than one timeframe step."""
    if len(bars) < 2:
        return 0
    step = timeframe_seconds(timeframe)
    return sum(
        1 for prev, cur in pairwise(bars) if (cur.timestamp - prev.timestamp).total_seconds() > step
    )


def _pad_target(values: ndarray, n: int) -> ndarray:
    """Right-pad with NaN so the target aligns with the rows it describes."""
    out = np.full(n, np.nan, dtype=np.float64)
    out[: len(values)] = values
    return out


class DatasetBuilder:
    """Builds the training matrix from stored bars read through an injected repository."""

    def __init__(self, repo: MarketDataRepository) -> None:
        self._repo = repo

    async def build(
        self,
        spec: FeatureSpec,
        exchange: str,
        symbol: str,
    ) -> tuple[FeatureMatrix, DatasetProvenance]:
        """The matrix (features + sign target + forward returns) and its provenance.

        The primary 1h timeframe must have stored bars; missing 4h/1d history is
        allowed and disclosed as all-NaN feature columns with ``bar_count = 0``.
        """
        reads: tuple[tuple[Timeframe, int], ...] = (
            (Timeframe.H1, spec.window_1h),
            (Timeframe.H4, spec.window_4h),
            (Timeframe.D1, spec.window_1d),
        )
        bars_by_tf = {
            tf: await self._repo.get_latest_bars(exchange, symbol, tf, window)
            for tf, window in reads
        }
        bars_1h = bars_by_tf[Timeframe.H1]
        if not bars_1h:
            available = await self._repo.get_bar_range(exchange, symbol, Timeframe.H1)
            first, last = (available[0], available[1]) if available else (None, None)
            raise NoMarketDataError(exchange, symbol, Timeframe.H1, None, None, first, last)

        matrix = build_features(
            spec,
            bars_1h,
            bars_by_tf[Timeframe.H4],
            bars_by_tf[Timeframe.D1],
        )
        signs, forward = build_target(
            np.array([float(b.close) for b in bars_1h], dtype=np.float64), spec.horizon
        )
        matrix = replace(
            matrix,
            target=_pad_target(signs, len(bars_1h)),
            forward_returns=_pad_target(forward, len(bars_1h)),
        )

        coverage_list = [
            await self._coverage(exchange, symbol, tf, bars_by_tf[tf]) for tf, _ in reads
        ]
        coverage: tuple[TimeframeCoverage, ...] = tuple(coverage_list)
        provenance = DatasetProvenance(
            exchange=exchange,
            symbol=symbol,
            spec_version=spec.spec_version,
            horizon=spec.horizon,
            coverage=coverage,
        )
        return matrix, provenance

    async def _coverage(
        self,
        exchange: str,
        symbol: str,
        timeframe: Timeframe,
        bars: list[OHLCV],
    ) -> TimeframeCoverage:
        if not bars:
            return TimeframeCoverage(
                timeframe=str(timeframe),
                bar_count=0,
                first_ts=None,
                last_ts=None,
                gap_count=0,
                sources={},
            )
        sources = await self._repo.get_source_counts(
            exchange, symbol, timeframe, bars[0].timestamp, bars[-1].timestamp
        )
        return TimeframeCoverage(
            timeframe=str(timeframe),
            bar_count=len(bars),
            first_ts=bars[0].timestamp,
            last_ts=bars[-1].timestamp,
            gap_count=_gap_count(bars, timeframe),
            sources=sources,
        )
