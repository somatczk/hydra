"""The worker's alpaca seams: the per-call client from the stored key and the per-job asset
check that records the asset id and the split adjustment (no network, no database)."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from cryptography.fernet import Fernet

import hydra.data.alpaca as alpaca_module
from hydra.backtest import worker
from hydra.core.encryption import encrypt
from hydra.data.alpaca import AlpacaClient
from hydra.execution import client_factory
from hydra.execution.errors import AlpacaNotConnectedError
from hydra.strategy.config import StrategyConfig

AAPL = {"id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415", "class": "us_equity", "symbol": "AAPL"}


class _Pool:
    """``exchange_credentials`` holding at most the alpaca row; records each read."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.reads = 0

    def acquire(self) -> _Pool:
        return self

    async def __aenter__(self) -> _Pool:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        assert sql == (
            "SELECT encrypted_key, encrypted_secret FROM exchange_credentials "
            "WHERE exchange_id = 'alpaca'"
        )
        self.reads += 1
        return self.row


@pytest.fixture(autouse=True)
def credential_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())


def _row() -> dict[str, Any]:
    return {"encrypted_key": encrypt("paper-key-id"), "encrypted_secret": encrypt("paper-secret")}


Handler = Callable[[httpx.Request], httpx.Response]


def _serve(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    monkeypatch.setattr(
        alpaca_module,
        "AlpacaClient",
        functools.partial(AlpacaClient, transport=httpx.MockTransport(handler)),
    )


def _config(exchange_id: str, symbol: str) -> StrategyConfig:
    return StrategyConfig(
        id="s",
        name="s",
        strategy_class="hydra.strategy.builtin.rule_based.RuleBasedStrategy",
        symbols=[symbol],
        exchange={"exchange_id": exchange_id, "market_type": "SPOT"},
    )


async def test_the_client_is_built_from_the_stored_key_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, str]] = []

    def account(request: httpx.Request) -> httpx.Response:
        seen.append((request.headers["APCA-API-KEY-ID"], request.headers["APCA-API-SECRET-KEY"]))
        return httpx.Response(200, json={"cash": "100000"})

    _serve(monkeypatch, account)
    pool = _Pool(_row())
    clients = worker._WorkerFetchClients(db_pool=pool)
    first, second = await clients.client("alpaca"), await clients.client("alpaca")
    assert first is not second  # never pooled: a reconnected key applies on the next fetch
    await first.fetch_balance()
    assert (pool.reads, seen) == (2, [("paper-key-id", "paper-secret")])


async def test_no_stored_key_is_alpaca_not_connected() -> None:
    with pytest.raises(AlpacaNotConnectedError) as err:
        await client_factory.build_alpaca_data_client(_Pool(None))
    assert err.value.code == "alpaca_not_connected"


async def test_the_asset_check_records_the_asset_id_and_the_split_adjustment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths: list[str] = []

    def assets(request: httpx.Request) -> httpx.Response:
        paths.append(f"{request.url.host}{request.url.path}")
        return httpx.Response(200, json=AAPL)

    _serve(monkeypatch, assets)
    venue = await worker._alpaca_provenance(_Pool(_row()), _config("alpaca", "AAPL/USD"))
    assert venue == {
        "alpaca_asset_id": "b0b6dd9d-8b9b-48a9-ba46-b9d54906e415",
        "adjustment": "split",
    }
    assert paths == ["paper-api.alpaca.markets/v2/assets/AAPL"]


async def test_a_crypto_venue_makes_no_asset_call() -> None:
    pool = _Pool(None)
    assert await worker._alpaca_provenance(pool, _config("binance", "BTCUSDT")) == {}
    assert pool.reads == 0


@pytest.mark.parametrize(
    ("row", "response", "phrase"),
    [
        (None, None, "AlpacaNotConnectedError"),
        (True, httpx.Response(200, json={"id": "x", "class": "crypto"}), "not us_equity"),
        (True, httpx.Response(404, json={"message": "asset not found"}), "BadSymbol"),
        (True, httpx.Response(401, json={"message": "unauthorized"}), "AuthenticationError"),
    ],
)
async def test_a_failed_asset_check_fails_the_job_by_name(
    monkeypatch: pytest.MonkeyPatch, row: Any, response: httpx.Response | None, phrase: str
) -> None:
    if response is not None:
        _serve(monkeypatch, lambda request: response)
    pool = _Pool(_row() if row else None)
    with pytest.raises(worker.JobError, match=f"check for AAPL/USD failed: .*{phrase}"):
        await worker._alpaca_provenance(pool, _config("alpaca", "AAPL/USD"))
