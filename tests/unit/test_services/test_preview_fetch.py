"""The preview's on-demand fetch engine: one per engine process, kill-switch aware.

``preview_fetch_engine`` is the engine process's construction (the worker builds its
own in ``hydra.backtest.worker``): exactly one ``OnDemandFetchService`` per process —
one weight budget, one set of per-dataset write locks (the one-instance contract) —
built lazily from the first bound ``data`` config, and ``None`` while the kill switch
is on. Construction itself never reaches the network: the REST clients are constructed
on the first real fetch.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hydra.core.config import DataConfig
from hydra.services import strategies as svc


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
