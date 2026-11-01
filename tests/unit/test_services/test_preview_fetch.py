"""The preview's on-demand fetch engine: one per engine process, kill-switch aware.

``preview_fetch_engine`` is the engine process's construction (the worker builds its
own in ``hydra.backtest.worker``): exactly one ``OnDemandFetchService`` per process —
one weight budget, one set of per-dataset write locks (the one-instance contract) —
built lazily from the first bound ``data`` config, and ``None`` while the kill switch
is on. Construction itself never reaches the network: the REST clients are constructed
on the first real fetch.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from hydra.core.config import DataConfig
from hydra.services import strategies as svc
from hydra.services.errors import ServiceError


@pytest.fixture(autouse=True)
def _reset_preview_engine() -> Iterator[None]:
    svc._PREVIEW_ENGINE = None
    yield
    svc._PREVIEW_ENGINE = None


def test_the_kill_switch_answers_none() -> None:
    assert svc.preview_fetch_engine(DataConfig(auto_fetch_enabled=False)) is None


def test_one_engine_per_process_is_built_once_and_memoized() -> None:
    first = svc.preview_fetch_engine(DataConfig())
    assert first is not None
    # the one-instance contract: every later call returns the same engine, whatever
    # config it arrives with — the knobs are frozen at first construction
    assert svc.preview_fetch_engine(DataConfig(fetch_weight_per_min=600)) is first


def test_the_kill_switch_after_construction_still_answers_none() -> None:
    assert svc.preview_fetch_engine(DataConfig()) is not None
    assert svc.preview_fetch_engine(DataConfig(auto_fetch_enabled=False)) is None


class _Credentials:
    """``exchange_credentials`` with no alpaca row."""

    def acquire(self) -> _Credentials:
        return self

    async def __aenter__(self) -> _Credentials:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> None:
        return None


class _Repo:
    """The dataset lock and the ledger status: all a forced US_EQUITY fetch touches first."""

    def __init__(self) -> None:
        self.statuses: list[str] = []

    @asynccontextmanager
    async def dataset_lock(self, *args: Any) -> AsyncIterator[_Repo]:
        yield self

    async def set_cache_status(self, *args: Any) -> None:
        self.statuses.append(args[3])


async def test_an_alpaca_preview_without_a_stored_key_is_422_alpaca_not_connected() -> None:
    """The preview builds alpaca's client from the stored key at fetch time: none stored
    is the same 422 the backtest preflight answers, never a 500 from inside the fetch."""
    engine = svc.preview_fetch_engine(DataConfig(), _Credentials())
    assert engine is not None
    body = svc.PreviewRequest(
        rules={"entry_long": {"operator": "AND", "conditions": []}},
        timeframe="1d",
        exchange_id="alpaca",
        symbol="AAPL/USD",
        bars_count=50,
    )
    repo = _Repo()
    with pytest.raises(ServiceError) as err:
        await svc._fetched_latest_bars(repo, body, engine)
    assert err.value.status_code == 422
    assert err.value.detail == {
        "code": "alpaca_not_connected",
        "message": "alpaca is not connected: store its paper key in Settings",
        "state": "invalid",
    }
    assert repo.statuses == ["fetching", "ready"]
