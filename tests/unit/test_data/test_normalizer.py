"""Tests for hydra.data.normalizer — OHLCV normalization, validation, anomaly detection."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from hydra.core.types import OHLCV
from hydra.data.normalizer import DataNormalizer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def normalizer() -> DataNormalizer:
    return DataNormalizer()


def _make_bar(
    open_: str = "42000.50",
    high: str = "42500.00",
    low: str = "41800.25",
    close: str = "42100.00",
    volume: str = "123.456",
    ts: datetime | None = None,
) -> OHLCV:
    """Helper to build an OHLCV bar quickly."""
    return OHLCV(
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal(volume),
        timestamp=ts or datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# normalize_ohlcv
# ---------------------------------------------------------------------------


class TestNormalizeOhlcv:
    """Tests for DataNormalizer.normalize_ohlcv()."""

    def test_valid_ccxt_data(self, normalizer: DataNormalizer) -> None:
        """Standard CCXT array is correctly converted."""
        raw = [1704067200000, 42000.50, 42500.00, 41800.25, 42100.00, 123.456]
        bar = normalizer.normalize_ohlcv(raw, "binance")

        assert isinstance(bar, OHLCV)
        assert bar.open == Decimal("42000.5")
        assert bar.high == Decimal("42500.0")
        assert bar.low == Decimal("41800.25")
        assert bar.close == Decimal("42100.0")
        assert bar.volume == Decimal("123.456")
        assert bar.timestamp.tzinfo is not None

    def test_decimal_precision_preserved(self, normalizer: DataNormalizer) -> None:
        """Decimal precision is maintained through conversion."""
        raw = [1704067200000, 0.00000001, 0.00000002, 0.00000001, 0.00000002, 0.00000001]
        bar = normalizer.normalize_ohlcv(raw, "binance")

        assert bar.open == Decimal("1E-8")
        assert bar.close == Decimal("2E-8")

    def test_integer_values(self, normalizer: DataNormalizer) -> None:
        """Integer values from the exchange are handled."""
        raw = [1704067200000, 42000, 42500, 41800, 42100, 100]
        bar = normalizer.normalize_ohlcv(raw, "bybit")

        assert bar.open == Decimal("42000")
        assert bar.volume == Decimal("100")

    def test_string_values(self, normalizer: DataNormalizer) -> None:
        """Some exchanges return strings — should still work."""
        raw = [1704067200000, "42000.50", "42500.00", "41800.25", "42100.00", "123.456"]
        bar = normalizer.normalize_ohlcv(raw, "kraken")

        assert bar.open == Decimal("42000.50")

    def test_timestamp_is_utc(self, normalizer: DataNormalizer) -> None:
        """Resulting timestamp has UTC timezone."""
        raw = [1704067200000, 42000, 42500, 41800, 42100, 100]
        bar = normalizer.normalize_ohlcv(raw, "binance")

        assert bar.timestamp.tzinfo == UTC
        assert bar.timestamp == datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)

    def test_too_few_elements_raises(self, normalizer: DataNormalizer) -> None:
        """Array with fewer than 6 elements raises ValueError."""
        with pytest.raises(ValueError, match="Expected 6-element"):
            normalizer.normalize_ohlcv([1704067200000, 42000], "binance")

    def test_invalid_value_raises(self, normalizer: DataNormalizer) -> None:
        """Non-numeric values raise ValueError."""
        with pytest.raises(ValueError, match="Failed to normalize"):
            normalizer.normalize_ohlcv([1704067200000, "bad", 2, 1, 1.5, 100], "binance")

    def test_extra_elements_ignored(self, normalizer: DataNormalizer) -> None:
        """Extra elements beyond the first 6 are ignored."""
        raw = [1704067200000, 42000, 42500, 41800, 42100, 100, "extra", 999]
        bar = normalizer.normalize_ohlcv(raw, "okx")

        assert bar.open == Decimal("42000")

    def test_all_major_exchanges_same_format(self, normalizer: DataNormalizer) -> None:
        """All supported exchanges use the same CCXT OHLCV array format."""
        raw = [1704067200000, 42000, 42500, 41800, 42100, 100]
        for exchange in ("binance", "bybit", "kraken", "okx"):
            bar = normalizer.normalize_ohlcv(raw, exchange)
            assert bar.open == Decimal("42000")
            assert bar.close == Decimal("42100")


# ---------------------------------------------------------------------------
# validate_bar
# ---------------------------------------------------------------------------


class TestValidateBar:
    """Tests for DataNormalizer.validate_bar()."""

    def test_valid_bar_passes(self, normalizer: DataNormalizer) -> None:
        """A well-formed bar passes all checks."""
        bar = _make_bar()
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is True
        assert errors == []

    def test_high_less_than_low(self, normalizer: DataNormalizer) -> None:
        """high < low is an error."""
        bar = _make_bar(high="41000.00", low="42000.00")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is False
        assert any("high" in e and "low" in e for e in errors)

    def test_high_less_than_open(self, normalizer: DataNormalizer) -> None:
        """high < open is an error."""
        bar = _make_bar(open_="43000.00", high="42500.00")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is False
        assert any("high" in e and "open" in e for e in errors)

    def test_high_less_than_close(self, normalizer: DataNormalizer) -> None:
        """high < close is an error."""
        bar = _make_bar(close="43000.00", high="42500.00")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is False
        assert any("high" in e and "close" in e for e in errors)

    def test_low_greater_than_open(self, normalizer: DataNormalizer) -> None:
        """low > open is an error."""
        bar = _make_bar(open_="41000.00", low="41800.25", high="42500.00")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is False
        assert any("low" in e and "open" in e for e in errors)

    def test_low_greater_than_close(self, normalizer: DataNormalizer) -> None:
        """low > close is an error."""
        bar = _make_bar(close="41000.00", low="41800.25", high="42500.00")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is False
        assert any("low" in e and "close" in e for e in errors)

    def test_negative_volume(self, normalizer: DataNormalizer) -> None:
        """Negative volume is an error."""
        bar = _make_bar(volume="-10")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is False
        assert any("negative volume" in e for e in errors)

    def test_zero_volume_valid(self, normalizer: DataNormalizer) -> None:
        """Zero volume passes validation (it is an anomaly, not invalid)."""
        bar = _make_bar(volume="0")
        is_valid, errors = normalizer.validate_bar(bar)

        assert is_valid is True
        assert errors == []

    def test_timestamp_with_timezone_passes(self, normalizer: DataNormalizer) -> None:
        """A bar with a timezone-aware timestamp passes."""
        bar = _make_bar(ts=datetime(2024, 1, 1, tzinfo=UTC))
        is_valid, _errors = normalizer.validate_bar(bar)

        assert is_valid is True

    def test_multiple_errors_reported(self, normalizer: DataNormalizer) -> None:
        """Multiple simultaneous errors are all reported."""
        bar = _make_bar(high="40000.00", low="43000.00", volume="-5")
        _, errors = normalizer.validate_bar(bar)

        assert len(errors) >= 2


# ---------------------------------------------------------------------------
# detect_anomaly
# ---------------------------------------------------------------------------


class TestDetectAnomaly:
    """Tests for DataNormalizer.detect_anomaly()."""

    def test_price_spike_detected(self, normalizer: DataNormalizer) -> None:
        """A > 20% price change between closes is flagged."""
        prev = _make_bar(
            close="40000.00",
            ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        )
        curr = _make_bar(
            close="50000.00",
            ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        )
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert any("price spike" in a for a in anomalies)

    def test_no_spike_within_threshold(self, normalizer: DataNormalizer) -> None:
        """A <= 20% price change is not flagged as a spike."""
        prev = _make_bar(
            close="40000.00",
            ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        )
        curr = _make_bar(
            close="44000.00",  # 10% change
            ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        )
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert not any("price spike" in a for a in anomalies)

    def test_zero_volume_detected(self, normalizer: DataNormalizer) -> None:
        """Zero volume is flagged."""
        prev = _make_bar(ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
        curr = _make_bar(volume="0", ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC))
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert "zero volume" in anomalies

    def test_negative_volume_detected(self, normalizer: DataNormalizer) -> None:
        """Negative volume is flagged."""
        prev = _make_bar(ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
        curr = _make_bar(volume="-5", ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC))
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert any("negative volume" in a for a in anomalies)

    def test_timestamp_not_advancing(self, normalizer: DataNormalizer) -> None:
        """Non-advancing timestamp is flagged."""
        ts = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
        prev = _make_bar(ts=ts)
        curr = _make_bar(ts=ts)  # same timestamp
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert any("timestamp not advancing" in a for a in anomalies)

    def test_timestamp_going_backwards(self, normalizer: DataNormalizer) -> None:
        """A bar with an earlier timestamp than its predecessor is flagged."""
        prev = _make_bar(ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC))
        curr = _make_bar(ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC))
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert any("timestamp not advancing" in a for a in anomalies)

    def test_none_previous_first_bar(self, normalizer: DataNormalizer) -> None:
        """First bar (previous=None) — only volume anomalies apply."""
        curr = _make_bar()
        anomalies = normalizer.detect_anomaly(curr, None)

        # Normal bar with volume > 0 should have no anomalies
        assert anomalies == []

    def test_none_previous_zero_volume(self, normalizer: DataNormalizer) -> None:
        """First bar with zero volume is still flagged."""
        curr = _make_bar(volume="0")
        anomalies = normalizer.detect_anomaly(curr, None)

        assert "zero volume" in anomalies

    def test_no_anomalies_for_normal_bars(self, normalizer: DataNormalizer) -> None:
        """Normal consecutive bars produce no anomalies."""
        prev = _make_bar(
            close="42000.00",
            ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        )
        curr = _make_bar(
            close="42100.00",
            ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        )
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert anomalies == []

    def test_price_spike_downward(self, normalizer: DataNormalizer) -> None:
        """A > 20% downward price move is also flagged."""
        prev = _make_bar(
            close="50000.00",
            ts=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        )
        curr = _make_bar(
            close="39000.00",  # -22%
            ts=datetime(2024, 1, 1, 0, 1, tzinfo=UTC),
        )
        anomalies = normalizer.detect_anomaly(curr, prev)

        assert any("price spike" in a for a in anomalies)
