"""Tests for hydra.data.backfill — paginated download (gap detection: tests/integration)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import ccxt
import pytest

from hydra.core.types import OHLCV, Timeframe
from hydra.data.backfill import ExchangeBackfillService
from tests.fixtures.binance_kline import FakeRestExchange

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ts(minute: int) -> datetime:
    """Shortcut to create a UTC datetime at a specific minute on 2024-01-01."""
    return datetime(2024, 1, 1, 0, minute, tzinfo=UTC)


# ---------------------------------------------------------------------------
# bulk_download
# ---------------------------------------------------------------------------


class _Repo:
    """Records what bulk_download persists."""

    def __init__(self) -> None:
        self.upserts: list[list[tuple[str, str, Timeframe, OHLCV]]] = []

    async def upsert_closed_bars(
        self, bars: list[tuple[str, str, Timeframe, OHLCV]]
    ) -> tuple[int, int]:
        self.upserts.append(list(bars))
        return (len(bars), 0)

    @property
    def stored(self) -> list[tuple[str, str, Timeframe, OHLCV]]:
        return [row for batch in self.upserts for row in batch]


def _row(ts: datetime, close: float = 42100.0) -> list[float]:
    return [ts.timestamp() * 1000, 42000.0, 42500.0, 41800.0, close, 100.0]


def _service(repo: _Repo, exchange: object) -> ExchangeBackfillService:
    return ExchangeBackfillService(
        repository=repo,
        exchange_factories={"binance": lambda: exchange},
    )


class _Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


_LATER = datetime(2030, 1, 1, tzinfo=UTC)


class TestBulkDownload:
    """Tests for ExchangeBackfillService.bulk_download()."""

    async def test_downloads_and_upserts(self) -> None:
        """Paginated fetch_ohlcv data is normalized and upserted."""
        repo = _Repo()
        exchange = FakeRestExchange([_row(_ts(m)) for m in range(3)])

        count = await _service(repo, exchange).bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(2),
            now=_LATER,
        )

        assert count == 3
        assert [(e, s, tf, b.timestamp) for e, s, tf, b in repo.stored] == [
            ("binance", "BTCUSDT", Timeframe.M1, _ts(m)) for m in range(3)
        ]

    async def test_respects_end_boundary(self) -> None:
        """Bars beyond the end timestamp are not stored."""
        repo = _Repo()
        exchange = FakeRestExchange([_row(_ts(m)) for m in range(3)])

        count = await _service(repo, exchange).bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(1),
            now=_LATER,
        )

        assert count == 2
        assert [b.timestamp for *_, b in repo.stored] == [_ts(0), _ts(1)]

    async def test_no_exchange_factory_raises(self) -> None:
        """Missing exchange factory raises ValueError."""
        service = ExchangeBackfillService(repository=AsyncMock())

        with pytest.raises(ValueError, match="No exchange factory"):
            await service.bulk_download(
                exchange_id="binance",
                symbol="BTCUSDT",
                timeframe=Timeframe.M1,
                start=datetime(2024, 1, 1, tzinfo=UTC),
                end=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
            )

    async def test_empty_response_stops_pagination(self) -> None:
        """An empty response from the exchange stops the download loop."""
        repo = _Repo()
        exchange = FakeRestExchange([])

        count = await _service(repo, exchange).bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=_ts(0),
            end=_ts(5),
            now=_LATER,
        )

        assert count == 0
        assert repo.upserts == []
        assert len(exchange.calls) == 1

    async def test_forming_candle_not_stored(self) -> None:
        """With now = T+30min on 1h, the T candle is still forming and is dropped (DA-4)."""
        t = datetime(2026, 2, 1, 10, tzinfo=UTC)
        repo = _Repo()
        rows = [_row(t - timedelta(hours=2)), _row(t - timedelta(hours=1)), _row(t, 1.0)]
        exchange = FakeRestExchange(rows)

        count = await _service(repo, exchange).bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.H1,
            start=t - timedelta(hours=2),
            end=t + timedelta(hours=1),
            now=t + timedelta(minutes=30),
        )

        assert count == 2
        assert [b.timestamp for *_, b in repo.stored] == [
            t - timedelta(hours=2),
            t - timedelta(hours=1),
        ]

    async def test_non_advancing_page_terminates_and_lower_bound(self) -> None:
        """The exchange returns the same page forever: one page is stored, then the loop ends."""
        page = [_row(_ts(m)) for m in range(5)]

        class _SamePage(FakeRestExchange):
            async def fetch_ohlcv(
                self,
                symbol: str,
                timeframe: str,
                since: int | None = None,
                limit: int | None = None,
            ) -> list[list[float]]:
                self.calls.append((symbol, timeframe, since or 0, limit or 0))
                return [list(r) for r in page]

        repo = _Repo()
        exchange = _SamePage()
        since = _ts(2)

        count = await _service(repo, exchange).bulk_download(
            exchange_id="binance",
            symbol="BTCUSDT",
            timeframe=Timeframe.M1,
            start=since,
            end=_ts(30),
            now=_LATER,
        )

        assert len(exchange.calls) == 2
        assert count == 3
        assert min(b.timestamp for *_, b in repo.stored) == since

    async def test_page_size_is_capped_at_1000(self) -> None:
        repo = _Repo()
        exchange = FakeRestExchange([_row(_ts(0))])
        await _service(repo, exchange).bulk_download(
            "binance", "BTCUSDT", Timeframe.M1, _ts(0), _ts(0), page_size=5000, now=_LATER
        )
        assert exchange.calls[0][3] == 1000

    async def test_rate_limit_retried(self) -> None:
        """A rate-limit error is retried after a backoff; the download completes."""
        repo = _Repo()
        exchange = FakeRestExchange([_row(_ts(m)) for m in range(3)])
        exchange.errors = [ccxt.RateLimitExceeded("429")]
        sleeps = _Sleeps()

        count = await _service(repo, exchange).bulk_download(
            "binance", "BTCUSDT", Timeframe.M1, _ts(0), _ts(2), now=_LATER, sleep=sleeps
        )

        assert count == 3
        assert len(repo.stored) == 3
        assert len(sleeps.calls) == 1
        assert sleeps.calls[0] >= 1.0

    async def test_ddos_protection_waits_at_least_a_minute(self) -> None:
        exchange = FakeRestExchange([_row(_ts(0))])
        exchange.errors = [ccxt.DDoSProtection("418")]
        sleeps = _Sleeps()
        await _service(_Repo(), exchange).bulk_download(
            "binance", "BTCUSDT", Timeframe.M1, _ts(0), _ts(0), now=_LATER, sleep=sleeps
        )
        assert sleeps.calls[0] >= 60.0

    async def test_persistent_failure_raises_after_max_retries(self) -> None:
        """Six consecutive network failures with max_retries=5 raise; nothing partial."""
        repo = _Repo()
        exchange = FakeRestExchange([_row(_ts(0))])
        exchange.errors = [ccxt.NetworkError("down") for _ in range(6)]
        sleeps = _Sleeps()

        with pytest.raises(ccxt.NetworkError):
            await _service(repo, exchange).bulk_download(
                "binance", "BTCUSDT", Timeframe.M1, _ts(0), _ts(0), now=_LATER, sleep=sleeps
            )

        assert len(sleeps.calls) == 5
        assert sleeps.calls == sorted(sleeps.calls)
        assert repo.upserts == []

    async def test_non_network_error_is_not_retried(self) -> None:
        exchange = FakeRestExchange([_row(_ts(0))])
        exchange.errors = [ccxt.BadSymbol("nope")]
        sleeps = _Sleeps()
        with pytest.raises(ccxt.BadSymbol):
            await _service(_Repo(), exchange).bulk_download(
                "binance", "BTCUSDT", Timeframe.M1, _ts(0), _ts(0), now=_LATER, sleep=sleeps
            )
        assert sleeps.calls == []
