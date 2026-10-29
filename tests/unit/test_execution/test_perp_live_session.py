"""The manager-level live perp seams: the start builds the perp executor behind the
gate, the fapi venue setup fires (one-way positions, ISOLATED margin, the strategy's
leverage re-validated against the live risk cap), and the seed is the perps budget.

The gate itself lives at the live surfaces (services/sessions, the executors, the
routes, the MCP tools — P4-E's tests pin the unarmed 409/403 refusals); this file
pins what the manager adds once the gate has passed: the budget read, the cap
re-validation, the venue setup, the seed and the session detail's perps block.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet

import hydra.core.encryption as encryption
from hydra.core.config import HydraConfig
from hydra.execution.client_factory import ClientFactory, SharedRestClients
from hydra.execution.paper_store import dumps_state
from hydra.execution.perp_live import (
    LivePerpOrderPoller,
    LivePerpTradingExecutor,
)
from hydra.execution.session_manager import SessionManager
from hydra.risk.market_rules import MarketRulesCache
from hydra.risk.pretrade import RiskConfig
from hydra.risk.service import RiskService
from hydra.risk.state import InMemoryRiskStateStore
from tests.fixtures.binance_futures_trading import FakeBinanceUsdmTradingClient
from tests.fixtures.binance_kline import FakeRestExchange, FakeWsExchange
from tests.unit.test_execution.session_harness import history_rows, markets_loader, write_strategy

D = Decimal
H = timedelta(hours=1)


class _RowConn:
    """The manager's start-path reads over a fake pool (writes are swallowed).

    ``stored`` and ``sessions`` are primeable by the recovery test: a row the
    first manager actually persisted (rebuilt from its live executor's own
    ``to_state``) and a running trading_sessions row.
    """

    def __init__(self, config: dict[str, str], credentials: dict[str, dict[str, Any]]) -> None:
        self._config = config
        self._credentials = credentials
        self.stored: dict[str, dict[str, Any]] = {}
        self.sessions: list[dict[str, Any]] = []

    async def fetchrow(self, query: str, *args: Any) -> Any:
        if "exchange_credentials" in query:
            return self._credentials.get(str(args[0]) or "")
        if "paper_accounts" in query:
            return self.stored.get(str(args[0]) or "")
        if "system_config" in query:
            key = next((k for k in self._config if f"'{k}'" in query), None)
            return None if key is None else {"value": self._config[key]}
        return None

    async def fetchval(self, query: str, *args: Any) -> Any:
        if "system_config" in query:
            key = next((k for k in self._config if f"'{k}'" in query), None)
            return None if key is None else self._config[key]
        return None

    async def fetch(self, query: str, *args: Any) -> list[Any]:
        if "trading_sessions" in query and "status = 'running'" in query:
            return list(self.sessions)
        return []

    async def execute(self, query: str, *args: Any) -> str:
        return "INSERT 0 1"

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield self


@dataclass
class _RowPool:
    conn: _RowConn

    @contextlib.asynccontextmanager
    async def acquire(self):
        yield self.conn


def _clients(fake: FakeBinanceUsdmTradingClient) -> ClientFactory:
    """The harness's fakes plus the keyed-client seam the perp start injects through."""
    rows = history_rows(datetime.now(UTC), 20, 100.0)
    ws: list[FakeWsExchange] = []

    def ws_factory(exchange_id: str) -> Any:
        def make() -> FakeWsExchange:
            ex = FakeWsExchange()
            ws.append(ex)
            return ex

        return make

    return ClientFactory(
        ws_factory=ws_factory,
        rest_pool=SharedRestClients(rest_factory=lambda eid, creds: FakeRestExchange(rows)),
        markets_loader=markets_loader,
        repository_factory=lambda _pool: None,
        history_window=20,
        trading_client_factory=lambda eid, creds, testnet, market_type=None: fake,
    )


async def _perp_manager(
    fake: FakeBinanceUsdmTradingClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    perps_capital: str = "500",
    live_capital: str = "600",
    risk_config: RiskConfig | None = None,
) -> SessionManager:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    environment = "testnet" if HydraConfig().trading.testnet else "mainnet"
    pool = _RowPool(
        _RowConn(
            {"perps_capital": perps_capital, "live_capital": live_capital},
            {
                "binanceusdm": {
                    "encrypted_key": encryption.encrypt("fapi-key"),
                    "encrypted_secret": encryption.encrypt("fapi-secret"),
                    "encrypted_passphrase": None,
                    "validated_at": "2026-10-29T00:00:00+00:00",
                    "validated_environment": environment,
                }
            },
        )
    )
    mgr = SessionManager(
        db_pool=pool,
        config=HydraConfig(),
        clients=_clients(fake),
        risk_store=InMemoryRiskStateStore(paper_capital=D("10000")),
        live_risk=RiskService(
            InMemoryRiskStateStore(paper_capital=D("600"), config=risk_config or RiskConfig()),
            scope="live",
            market_rules=MarketRulesCache(loader=markets_loader),
        ),
    )
    await mgr.startup()
    return mgr


async def _start_perp(
    mgr: SessionManager, strategies_dir: Path, sid: str, *, leverage: int = 2
) -> str:
    write_strategy(
        strategies_dir,
        sid,
        exchange_id="binanceusdm",
        market_type="FUTURES",
        leverage=leverage,
    )
    return await mgr.start_session(sid, "live")


async def _shutdown(mgr: SessionManager) -> None:
    for session in mgr.list_sessions():
        if session.status == "running":
            await mgr.stop_session(session.session_id)
    await mgr.graceful_shutdown()


class TestLivePerpStart:
    async def test_the_gate_positive_path_builds_the_perp_executor(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        mgr = await _perp_manager(fake, monkeypatch)
        try:
            session_id = await _start_perp(mgr, strategies_dir, "perp-live")
            session = mgr.get_session(session_id)
            assert session is not None
            assert isinstance(session._executor, LivePerpTradingExecutor)
            assert session.exchange_id == "binanceusdm"
            assert session.paper_capital == D("500")  # min(wallet 10000, perps_capital 500)
            assert fake.set_position_mode_calls == [False]  # one-way, once
            assert fake.set_margin_mode_calls == [("BTCUSDT", "isolated")]
            assert fake.set_leverage_calls == [("BTCUSDT", 2)]
            detail = await mgr.get_session_detail(session_id)
            assert detail["perps"]["leverage"] == 2
            assert detail["perps"]["margin_mode"] == "isolated"
            assert detail["trading_mode"] == "live"

            # the wiring treats the perp executor as live: the perp poller runs,
            # _is_live() is True and the dead-man coverage producer answers per symbol
            runtime = session._runtime
            assert runtime is not None
            assert isinstance(runtime._poller, LivePerpOrderPoller)
            assert runtime._is_live()
            assert await runtime._live_coverage() == {"BTCUSDT": None}
        finally:
            await _shutdown(mgr)

    async def test_the_seed_is_the_budget_when_the_wallet_is_bigger(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="120")
        mgr = await _perp_manager(fake, monkeypatch)
        try:
            session_id = await _start_perp(mgr, strategies_dir, "perp-small", leverage=3)
            session = mgr.get_session(session_id)
            assert session is not None
            assert session.paper_capital == D("120")  # min(wallet 120, perps_capital 500)
            assert fake.set_leverage_calls == [("BTCUSDT", 3)]
        finally:
            await _shutdown(mgr)

    async def test_the_leverage_cap_is_revalidated_at_start(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        mgr = await _perp_manager(fake, monkeypatch, risk_config=RiskConfig(max_perp_leverage=2))
        try:
            with pytest.raises(ValueError, match="leverage 3x is outside 1-2x"):
                await _start_perp(mgr, strategies_dir, "perp-over-cap", leverage=3)
            assert mgr.list_sessions() == []
            assert fake.set_position_mode_calls == []
        finally:
            await mgr.graceful_shutdown()

    async def test_no_free_wallet_refuses_the_start(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="0")
        mgr = await _perp_manager(fake, monkeypatch)
        try:
            with pytest.raises(ValueError, match="no free USDT"):
                await _start_perp(mgr, strategies_dir, "perp-broke")
            assert mgr.list_sessions() == []
        finally:
            await mgr.graceful_shutdown()

    async def test_a_perps_capital_row_at_zero_refuses(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        mgr = await _perp_manager(fake, monkeypatch, perps_capital="0")
        try:
            with pytest.raises(Exception, match="perps_capital must be > 0"):
                await _start_perp(mgr, strategies_dir, "perp-nocap")
            assert mgr.list_sessions() == []
        finally:
            await mgr.graceful_shutdown()

    async def test_a_venue_setup_failure_refuses_and_builds_nothing(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeBinanceUsdmTradingClient(wallet="10000", dual_side_position=True)

        async def _boom() -> Any:
            raise RuntimeError("fapi positionSide/dual refused with positions open")

        fake.set_one_way_position_mode = _boom
        mgr = await _perp_manager(fake, monkeypatch)
        try:
            with pytest.raises(Exception, match="venue setup failed"):
                await _start_perp(mgr, strategies_dir, "perp-dual")
            assert mgr.list_sessions() == []
        finally:
            await mgr.graceful_shutdown()


class TestLivePerpRecovery:
    async def test_a_running_perp_row_resumes_through_the_perp_executor(
        self, strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The start-vs-recovery venue symmetry (the P4-C A1 fix's lesson): a live
        binanceusdm row recovers through the PERP executor and re-applies the fapi
        venue setup — never the default-SPOT validation that errored it."""
        fake = FakeBinanceUsdmTradingClient(wallet="10000")
        mgr = await _perp_manager(fake, monkeypatch)
        session_id = await _start_perp(mgr, strategies_dir, "perp-rec")
        session = mgr.get_session(session_id)
        assert session is not None
        executor = session._executor
        assert isinstance(executor, LivePerpTradingExecutor)
        state = executor.to_state()
        version = executor._state_version
        pool = mgr._db_pool
        assert isinstance(pool, _RowPool)
        pool.conn.stored[session_id] = {
            "session_id": session_id,
            "strategy_id": session.strategy_id,
            "exchange_id": "binanceusdm",
            "quote_asset": "USDT",
            "state": dumps_state(state),
            "state_version": version,
        }
        pool.conn.sessions = [
            {
                "id": session_id,
                "strategy_id": session.strategy_id,
                "trading_mode": "live",
                "exchange_id": "binanceusdm",
                "symbols": list(session.symbols),
                "timeframe": session.timeframe,
                "paper_capital": session.paper_capital,
                "started_at": session.started_at,
                "strategy_config": session.strategy_config,
                "created_by": session.created_by,
            }
        ]
        setup_calls_before = len(fake.set_leverage_calls)
        await _shutdown(mgr)  # the row stays 'running' for recovery

        recovered = SessionManager(
            db_pool=pool,
            config=HydraConfig(),
            clients=_clients(fake),
            risk_store=InMemoryRiskStateStore(paper_capital=D("10000")),
            live_risk=RiskService(
                InMemoryRiskStateStore(paper_capital=D("600"), config=RiskConfig()),
                scope="live",
                market_rules=MarketRulesCache(loader=markets_loader),
            ),
        )
        await recovered.startup()
        try:
            restored = recovered.get_session(session_id)
            assert restored is not None
            assert restored.status == "running"
            assert isinstance(restored._executor, LivePerpTradingExecutor)
            assert restored._executor.leverage == 2
            assert fake.set_leverage_calls[setup_calls_before:] == [("BTCUSDT", 2)]
            assert ("BTCUSDT", "isolated") in fake.set_margin_mode_calls[setup_calls_before:]
        finally:
            await _shutdown(recovered)
