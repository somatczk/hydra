"""Raw exchange data normalization and validation.

Converts raw CCXT data arrays into typed ``OHLCV`` dataclasses with full
``Decimal`` precision, validates bar integrity, and detects anomalies.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from hydra.core.logging import get_logger
from hydra.core.types import OHLCV, ExchangeId

logger = get_logger(__name__)

# Anomaly detection thresholds
_PRICE_SPIKE_PCT = Decimal("0.20")  # 20% price spike threshold


class DataNormalizer:
    """Normalize, validate, and detect anomalies in exchange market data."""

    # ------------------------------------------------------------------
    # Normalization
    # ------------------------------------------------------------------

    def normalize_ohlcv(self, raw_data: list[Any], exchange_id: ExchangeId) -> OHLCV:
        """Convert a raw CCXT OHLCV array to a typed ``OHLCV`` dataclass.

        CCXT returns arrays of ``[timestamp_ms, open, high, low, close, volume]``.
        All numeric values are converted to ``Decimal`` for financial precision.

        Parameters
        ----------
        raw_data:
            Six-element list ``[timestamp_ms, open, high, low, close, volume]``
            as returned by ``ccxt.exchange.fetch_ohlcv()`` or
            ``ccxt.pro.exchange.watch_ohlcv()``.
        exchange_id:
            Identifier for the originating exchange (used for logging context).

        Returns
        -------
        OHLCV
            Typed, immutable bar with ``Decimal`` precision.

        Raises
        ------
        ValueError
            If *raw_data* does not contain exactly six elements or values
            cannot be converted.
        """
        if len(raw_data) < 6:
            msg = (
                f"Expected 6-element OHLCV array from {exchange_id}, "
                f"got {len(raw_data)}: {raw_data!r}"
            )
            raise ValueError(msg)

        try:
            timestamp_ms = raw_data[0]
            ts = datetime.fromtimestamp(timestamp_ms / 1000, tz=UTC)

            open_ = Decimal(str(raw_data[1]))
            high = Decimal(str(raw_data[2]))
            low = Decimal(str(raw_data[3]))
            close = Decimal(str(raw_data[4]))
            volume = Decimal(str(raw_data[5]))
        except (InvalidOperation, TypeError, OverflowError, OSError) as exc:
            msg = f"Failed to normalize OHLCV from {exchange_id}: {raw_data!r}"
            raise ValueError(msg) from exc

        return OHLCV(
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            timestamp=ts,
        )

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def validate_bar(self, ohlcv: OHLCV) -> tuple[bool, list[str]]:
        """Check data integrity of a single OHLCV bar.

        Returns a tuple of ``(is_valid, list_of_errors)``.  An empty error
        list means the bar passed all checks.

        Checks performed:
        - ``high >= low``
        - ``high >= open``
        - ``high >= close``
        - ``low <= open``
        - ``low <= close``
        - ``volume >= 0``
        - timestamp has UTC timezone info
        """
        errors: list[str] = []

        if ohlcv.high < ohlcv.low:
            errors.append(f"high ({ohlcv.high}) < low ({ohlcv.low})")

        if ohlcv.high < ohlcv.open:
            errors.append(f"high ({ohlcv.high}) < open ({ohlcv.open})")

        if ohlcv.high < ohlcv.close:
            errors.append(f"high ({ohlcv.high}) < close ({ohlcv.close})")

        if ohlcv.low > ohlcv.open:
            errors.append(f"low ({ohlcv.low}) > open ({ohlcv.open})")

        if ohlcv.low > ohlcv.close:
            errors.append(f"low ({ohlcv.low}) > close ({ohlcv.close})")

        if ohlcv.volume < Decimal("0"):
            errors.append(f"negative volume ({ohlcv.volume})")

        if ohlcv.timestamp.tzinfo is None:
            errors.append("timestamp missing timezone info")

        return (len(errors) == 0, errors)

    # ------------------------------------------------------------------
    # Anomaly detection
    # ------------------------------------------------------------------

    def detect_anomaly(
        self,
        current: OHLCV,
        previous: OHLCV | None,
    ) -> list[str]:
        """Detect anomalies by comparing the current bar to the previous one.

        Checks performed:
        - Price spike > 20% between consecutive closes
        - Zero volume
        - Negative volume
        - Timestamp not advancing (current <= previous)

        Parameters
        ----------
        current:
            The bar to evaluate.
        previous:
            The preceding bar, or ``None`` for the first bar in a series.

        Returns
        -------
        list[str]
            List of anomaly descriptions.  Empty if no anomalies detected.
        """
        anomalies: list[str] = []

        # Volume checks (apply even without a previous bar)
        if current.volume == Decimal("0"):
            anomalies.append("zero volume")

        if current.volume < Decimal("0"):
            anomalies.append(f"negative volume ({current.volume})")

        if previous is None:
            return anomalies

        # Price spike check
        if previous.close != Decimal("0"):
            change = abs(current.close - previous.close) / previous.close
            if change > _PRICE_SPIKE_PCT:
                pct = change * 100
                anomalies.append(
                    f"price spike: {pct:.2f}% change ({previous.close} -> {current.close})"
                )

        # Timestamp must advance
        if current.timestamp <= previous.timestamp:
            anomalies.append(
                f"timestamp not advancing: "
                f"{current.timestamp.isoformat()} <= {previous.timestamp.isoformat()}"
            )

        return anomalies
