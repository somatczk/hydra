"""The perp backtest's funding preflight: top-up then load, honestly (P4-C).

No egress: the client is a ccxt-shaped fake and the pool mirrors the funding
writer's insert-if-absent contract plus the loader's SELECT. The three states
are pinned — ``fetch_skipped`` (auto-fetch off), ``ensured`` (top-up ran) and
``fetch_failed`` (the stored rows still load) — and the loaded rows are exactly
the stored ones, never a fabricated rate.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from hydra.backtest.perp_funding import prepare_backtest_funding
from hydra.core.types import ExchangeId

T0 = datetime(2026, 1, 1, 0, tzinfo=UTC)
H8 = timedelta(hours=8)
D = Decimal
EXCHANGE: ExchangeId = "binanceusdm"

STORED = [(T0 + i * H8, D("0.0001")) for i in range(6)]


class _FakeClient:
    """ccxt-shaped funding history for one page; raises when *fail* is set."""

    def __init__(
        self, entries: list[tuple[datetime, str]] | None = None, *, fail: bool = False
    ) -> None:
        self.entries = entries or []
        self.fail = fail

    async def fetch_funding_rate_history(
        self,
        symbol: str,
        since: int | None = None,
        limit: int | None = None,
        params: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if self.fail:
            raise RuntimeError("fapi unreachable")
        until = (params or {}).get("until")
        return [
            {
                "info": {"symbol": symbol, "fundingRate": rate},
                "symbol": f"{symbol}/USDT:USDT",
                "fundingRate": float(rate),
                "timestamp": int(ts.timestamp() * 1000),
                "datetime": ts.isoformat(),
            }
            for ts, rate in self.entries
            if (since is None or int(ts.timestamp() * 1000) >= since)
            and (until is None or int(ts.timestamp() * 1000) <= until)
        ]


class _FakePool:
    """Mirrors the funding writer's advisory lock + insert-if-absent and the loader."""

    def __init__(self, stored: list[tuple[datetime, D]] = ()) -> None:
        self.stored: dict[datetime, D] = {ts: rate for ts, rate in stored}

    def acquire(self) -> _FakeConn:
        return _FakeConn(self)


class _FakeConn:
    def __init__(self, pool: _FakePool) -> None:
        self._pool = pool

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def transaction(self) -> _FakeConn:
        return self

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "pg_try_advisory_xact_lock" in sql:
            return True
        raise AssertionError(f"unexpected fetchval: {sql}")

    async def fetch(self, sql: str, *args: Any) -> Any:
        if "INSERT INTO ts.funding_rates" in sql:
            symbols, rates, _next, timestamps = args[1], args[2], args[3], args[4]
            inserted: list[int] = []
            for _symbol, rate, timestamp in zip(symbols, rates, timestamps, strict=True):
                if timestamp not in self._pool.stored:
                    self._pool.stored[timestamp] = D(str(rate))
                    inserted.append(1)
            return inserted
        if "FROM ts.funding_rates" in sql:
            start, end = args[2], args[3]
            return [
                {"timestamp": ts, "rate": rate}
                for ts, rate in sorted(self._pool.stored.items())
                if start <= ts <= end
            ]
        raise AssertionError(f"unexpected fetch: {sql}")


START, END = T0 - H8, T0 + 6 * H8


async def test_no_provider_skips_the_fetch_and_loads_the_stored_rows() -> None:
    pool = _FakePool(STORED)
    funding, state = await prepare_backtest_funding(pool, None, "BTCUSDT", START, END)
    assert state == "fetch_skipped"
    assert funding == {"BTCUSDT": STORED}


async def test_a_provider_tops_the_window_up_then_loads_it() -> None:
    pool = _FakePool(STORED[:2])
    fetched = [(T0 + i * H8, "0.0001") for i in range(2, 6)]
    client = _FakeClient(fetched)
    funding, state = await prepare_backtest_funding(
        pool, lambda _eid: _ok(client), "BTCUSDT", START, END
    )
    assert state == "ensured"
    assert funding == {"BTCUSDT": STORED}


async def test_a_failing_client_keeps_the_stored_rows_and_says_fetch_failed() -> None:
    pool = _FakePool(STORED[:2])
    funding, state = await prepare_backtest_funding(
        pool, lambda _eid: _boom(), "BTCUSDT", START, END
    )
    assert state == "fetch_failed"
    assert funding == {"BTCUSDT": STORED[:2]}


async def test_a_failing_top_up_keeps_the_stored_rows_and_says_fetch_failed() -> None:
    pool = _FakePool(STORED[:2])
    funding, state = await prepare_backtest_funding(
        pool, lambda _eid: _ok(_FakeClient(fail=True)), "BTCUSDT", START, END
    )
    assert state == "fetch_failed"
    assert funding == {"BTCUSDT": STORED[:2]}


async def _ok(client: Any) -> Any:
    return client


async def _boom() -> Any:
    raise RuntimeError("fapi unreachable")
