"""Restart on real TimescaleDB: a paper session with an open position resumes, live rows stop.

The engine side is real (SessionManager, PgRiskStateStore, paper_store, the feed's bar repository);
only the ccxt clients are fakes (no exchange is called).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import asyncpg
import yaml
from fastapi import FastAPI

from hydra.core.config import HydraConfig
from hydra.core.types import OHLCV
from hydra.dashboard import lifespan
from hydra.data.migrations.runner import upgrade_to_head
from hydra.execution import paper_store
from hydra.execution.client_factory import ClientFactory, bars_repository
from hydra.execution.paper_trading import PaperTradingExecutor
from hydra.execution.perp_trading import PerpPaperTradingExecutor
from hydra.execution.session_manager import SessionManager
from hydra.risk.pretrade import RiskConfig
from hydra.risk.state import InMemoryRiskStateStore
from hydra.services import strategies as svc
from hydra.strategy.config import StrategyConfig, config_hash
from tests.unit.test_execution.session_harness import (
    Harness,
    executor,
    held,
    rule,
    settle,
    write_strategy,
)

D = Decimal


def _clients(h: Harness) -> ClientFactory:
    fake = h.clients()
    return ClientFactory(
        ws_factory=fake.ws_factory,
        rest_pool=fake.rest_pool,
        markets_loader=fake.markets_loader,
        repository_factory=bars_repository,
        history_window=fake.history_window,
    )


async def restart_scenario(dsn: str, strategies_dir: Path) -> None:
    """Start, fill, shut down, restart through the lifespan step; check DB and memory agree."""
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        h = Harness(strategies_dir)
        sid = write_strategy(
            strategies_dir, "pg-rt", entry_long=rule(("less_than", 100.5), ("greater_than", 99.5))
        )
        first = SessionManager(db_pool=pool, config=HydraConfig(), clients=_clients(h))
        await first.startup()
        session_id = await first.start_session(sid, "paper", D("1000"))
        session = first.get_session(session_id)
        assert session is not None
        await h.bar(session, "100")
        assert await held(session) == D("1.00000")
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO trading_sessions (id, strategy_id, trading_mode, status, exchange_id,"
                " symbols, timeframe) VALUES ('live-1', 'pg-rt', 'live', 'running', 'binance',"
                " ARRAY['BTCUSDT'], '1h')"
            )
            stored = await conn.fetchval(
                "SELECT count(*) FROM ts.ohlcv_1m WHERE symbol = 'BTCUSDT' AND timeframe = '1h'"
            )
        assert stored == 20  # warm-up REST fill persisted for the backtester
        await first.graceful_shutdown()

        app = FastAPI()
        app.state.db_pool = pool
        await lifespan._init_session_manager(app, HydraConfig(), _clients(h))
        second: SessionManager = app.state.session_manager
        await settle()
        try:
            resumed = second.get_session(session_id)
            assert resumed is not None and resumed.status == "running" and resumed.task_alive
            ex = executor(resumed)
            assert isinstance(ex, PaperTradingExecutor)
            assert await held(resumed) == D("1.00000")
            assert (await ex.get_balance())["USDT"] == D("899.7999")
            stops = await ex.fetch_open_orders("BTCUSDT")
            assert [(o["type"], o["amount"], o["stop_price"]) for o in stops] == [
                ("STOP_MARKET", 1.0, 95.095)
            ]
            live = second.get_session("live-1")
            # live rows now resume when they can; without stored credentials this one
            # fails its rebuild and is marked error (never a zombie)
            assert live is not None and live.status == "error" and not live.task_alive
            async with pool.acquire() as conn:
                rows = {
                    r["id"]: (r["status"], r["error_message"])
                    for r in await conn.fetch(
                        "SELECT id, status, error_message FROM trading_sessions"
                    )
                }
                position = await conn.fetchrow(
                    "SELECT quantity, avg_entry_price FROM positions WHERE session_id = $1",
                    session_id,
                )
            assert rows[session_id] == ("running", None)
            assert rows["live-1"][0] == "error"
            assert "live setup failed" in rows["live-1"][1]
            assert position is not None
            assert (position["quantity"], position["avg_entry_price"]) == (D("1"), D("100.1"))

            await second.stop_session(session_id)
            async with pool.acquire() as conn:
                status = await conn.fetchval(
                    "SELECT status FROM trading_sessions WHERE id = $1", session_id
                )
                open_rows = await conn.fetchval(
                    "SELECT count(*) FROM positions WHERE session_id = $1", session_id
                )
                carried = await conn.fetchval(
                    "SELECT carried_pnl FROM risk_state WHERE scope = 'paper'"
                )
            assert (status, open_rows) == ("stopped", 0)
            assert carried == ex.get_account_snapshot().equity - D("1000")
        finally:
            await second.graceful_shutdown()
    finally:
        await pool.close()


async def test_restart_resumes_paper_and_stops_live(
    fresh_database: str, strategies_dir: Path
) -> None:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    await restart_scenario(fresh_database, strategies_dir)


async def test_recovery_uses_snapshot(fresh_database: str, strategies_dir: Path) -> None:
    """A redeploy after a YAML edit resumes the version the session started with, not the file."""
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        h = Harness(strategies_dir)
        sid = write_strategy(strategies_dir, "pg-snap", entry_long=rule(("less_than", 0.0)))
        path = strategies_dir / f"{sid}.yaml"
        data = yaml.safe_load(path.read_text())
        data["parameters"]["rsi_period"] = 14
        path.write_text(yaml.safe_dump(data))
        v1_hash = config_hash(StrategyConfig.model_validate({**data, "enabled": True}))

        first = SessionManager(db_pool=pool, config=HydraConfig(), clients=_clients(h))
        await first.startup()
        session_id = await first.start_session(sid, "paper", D("1000"))
        session = first.get_session(session_id)
        assert session is not None and session.config_hash == v1_hash
        await first.graceful_shutdown()

        # Edit the file after shutdown: a running session must not pick this up on restart.
        data = yaml.safe_load(path.read_text())
        data["parameters"]["rsi_period"] = 21
        path.write_text(yaml.safe_dump(data))
        v2_hash = config_hash(StrategyConfig.model_validate({**data, "enabled": True}))
        assert v2_hash != v1_hash

        app = FastAPI()
        app.state.db_pool = pool
        await lifespan._init_session_manager(app, HydraConfig(), _clients(h))
        second: SessionManager = app.state.session_manager
        await settle()
        try:
            recovered = second.get_session(session_id)
            assert recovered is not None and recovered.status == "running"
            assert recovered.config_hash == v1_hash
            runtime = recovered._runtime
            assert runtime is not None
            assert runtime.cfg.parameters["rsi_period"] == 14

            detail = await svc.get_strategy(pool, sid)
            (running,) = detail["running_sessions"]
            assert running["session_id"] == session_id
            assert running["stale"] is True
            assert detail["config_hash"] == v2_hash

            result = await second.reload_config(sid)
            assert (result.from_hash, result.to_hash) == (v1_hash, v2_hash)
            await h.bar(recovered, "100")
            row_hash = await pool.fetchval(
                "SELECT config_hash FROM trading_sessions WHERE id = $1", session_id
            )
            assert row_hash == v2_hash
            assert runtime.cfg.parameters["rsi_period"] == 21
        finally:
            await second.graceful_shutdown()

        # A second restart recovers 21: the snapshot was updated by the reload.
        app3 = FastAPI()
        app3.state.db_pool = pool
        await lifespan._init_session_manager(app3, HydraConfig(), _clients(h))
        third: SessionManager = app3.state.session_manager
        await settle()
        try:
            resumed_again = third.get_session(session_id)
            assert resumed_again is not None and resumed_again.config_hash == v2_hash
            runtime3 = resumed_again._runtime
            assert runtime3 is not None and runtime3.cfg.parameters["rsi_period"] == 21
        finally:
            await third.graceful_shutdown()
    finally:
        await pool.close()


async def test_recovery_of_pre_snapshot_row_falls_back_to_the_file(
    fresh_database: str, strategies_dir: Path
) -> None:
    """A NULL ``strategy_config`` row (pre-P2) recovers from the YAML file, as before."""
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    pool = await asyncpg.create_pool(fresh_database, min_size=1, max_size=4)
    try:
        h = Harness(strategies_dir)
        sid = write_strategy(strategies_dir, "pg-presnap", entry_long=rule(("less_than", 0.0)))
        path = strategies_dir / f"{sid}.yaml"
        data = yaml.safe_load(path.read_text())
        data["parameters"]["rsi_period"] = 14
        path.write_text(yaml.safe_dump(data))

        first = SessionManager(db_pool=pool, config=HydraConfig(), clients=_clients(h))
        await first.startup()
        session_id = await first.start_session(sid, "paper", D("1000"))
        await first.graceful_shutdown()

        # A pre-P2 row: the paper state exists, but no config snapshot was stored yet.
        await pool.execute(
            "UPDATE trading_sessions SET strategy_config = NULL, config_hash = NULL WHERE id = $1",
            session_id,
        )
        # The file moved on after the row was written; with no snapshot the file wins.
        data = yaml.safe_load(path.read_text())
        data["parameters"]["rsi_period"] = 21
        path.write_text(yaml.safe_dump(data))
        file_hash = config_hash(StrategyConfig.model_validate({**data, "enabled": True}))

        app = FastAPI()
        app.state.db_pool = pool
        await lifespan._init_session_manager(app, HydraConfig(), _clients(h))
        mgr: SessionManager = app.state.session_manager
        await settle()
        try:
            recovered = mgr.get_session(session_id)
            assert recovered is not None and recovered.status == "running"
            runtime = recovered._runtime
            assert runtime is not None
            assert runtime.cfg.parameters["rsi_period"] == 21  # from the file, not a snapshot
            assert recovered.strategy_config is not None  # backfilled on recovery
            assert recovered.config_hash == file_hash
            assert await held(recovered) == D("0")  # entry rule never fires; nothing was sold
        finally:
            await mgr.graceful_shutdown()
    finally:
        await pool.close()


async def perp_restart_scenario(dsn: str, strategies_dir: Path) -> None:
    """P4-C / the #211 A1 fix: a hand-written binanceusdm/FUTURES YAML starts AND recovers.

    Pre-fix the start rode the SPOT PaperTradingExecutor while recovery's default-SPOT
    ``validate_venue`` errored the same row after a restart; post-fix both paths run the
    perp executor — with leverage, margin and funding accrual from ``ts.funding_rates``.
    """
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        h = Harness(strategies_dir)
        sid = write_strategy(
            strategies_dir,
            "pg-perp",
            entry_long=rule(("less_than", 100.5), ("greater_than", 99.5)),
            exchange_id="binanceusdm",
            market_type="FUTURES",
            leverage=2,
        )
        first = SessionManager(db_pool=pool, config=HydraConfig(), clients=_clients(h))
        await first.startup()
        session_id = await first.start_session(sid, "paper", D("1000"))
        session = first.get_session(session_id)
        assert session is not None
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        await h.bar(session, "100", exchange_id="binanceusdm")
        snap = ex.get_account_snapshot()
        # 10% sizing x 2x leverage: 2 BTC at ~100 (slipped taker), half the notional as margin
        assert snap.leverage == 2 and snap.base_totals == {"BTC": D("2.00000")}
        wallet_after_entry = snap.wallet_available
        margin_after_entry = snap.margin_used

        # a settled funding row inside the next bar's window: the PG reader accrues it
        settled = h.end + timedelta(hours=1, minutes=30)
        async with pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO ts.funding_rates (exchange, symbol, rate, next_funding_time,"
                " timestamp) VALUES ('binanceusdm', 'BTCUSDT', 0.0001, NULL, $1)",
                settled,
            )
        await h.bar(session, "100", exchange_id="binanceusdm")
        snap = ex.get_account_snapshot()
        assert snap.funding_applied == 1
        # 2 BTC x 0.0001 x 100 (the mark carried into the bar) = 0.02 paid by the long
        assert snap.margin_used == margin_after_entry - D("0.02")
        assert snap.wallet_available == wallet_after_entry
        await first.graceful_shutdown()

        app = FastAPI()
        app.state.db_pool = pool
        await lifespan._init_session_manager(app, HydraConfig(), _clients(h))
        second: SessionManager = app.state.session_manager
        await settle()
        try:
            resumed = second.get_session(session_id)
            assert resumed is not None
            assert resumed.status == "running" and resumed.task_alive  # the A1 asymmetry is dead
            ex2 = resumed._executor
            assert isinstance(ex2, PerpPaperTradingExecutor)
            snap2 = ex2.get_account_snapshot()
            assert (snap2.leverage, snap2.funding_applied) == (2, 1)
            assert snap2.base_totals == {"BTC": D("2.00000")}
            assert snap2.margin_used == margin_after_entry - D("0.02")
            stops = await ex2.fetch_open_orders("BTCUSDT")
            assert stops and all(o["side"] == "SELL" for o in stops)
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, error_message FROM trading_sessions WHERE id = $1",
                    session_id,
                )
            assert row is not None and row["status"] == "running" and row["error_message"] is None
            await second.stop_session(session_id)
            snap3 = ex2.get_account_snapshot()
            assert snap3.base_totals == {"BTC": D("0")} and snap3.margin_used == D("0")
        finally:
            await second.graceful_shutdown()
    finally:
        await pool.close()


async def test_perp_session_starts_and_recovers_consistently(
    fresh_database: str, strategies_dir: Path
) -> None:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    await perp_restart_scenario(fresh_database, strategies_dir)


async def perp_liquidation_restart_scenario(dsn: str, strategies_dir: Path) -> None:
    """The P4-C review A1 fix: a liquidated perp session persists and restores halted.

    The crash window is real: the liquidation bar persists the halted account inside
    ``on_closed_bar`` (the fill, the closed position, ``halted`` in the state), while the
    session row is still ``running`` — a process death right there must not resurrect the
    liquidated position as a running session.
    """
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4)
    try:
        h = Harness(strategies_dir)
        sid = write_strategy(
            strategies_dir,
            "pg-perp-liq",
            entry_long=rule(("greater_than", 0.0)),
            risk={"stop_loss_method": "fixed_pct", "stop_loss_value": 45.0},
            exchange_id="binanceusdm",
            market_type="FUTURES",
            leverage=3,
        )
        first = SessionManager(
            db_pool=pool,
            config=HydraConfig(),
            clients=_clients(h),
            risk_store=InMemoryRiskStateStore(
                paper_capital=D("1000"), config=RiskConfig(max_risk_per_trade=D("0.5"))
            ),
        )
        await first.startup()
        session_id = await first.start_session(sid, "paper", D("1000"))
        session = first.get_session(session_id)
        assert session is not None
        await h.bar(session, "100", exchange_id="binanceusdm")
        ex = session._executor
        assert isinstance(ex, PerpPaperTradingExecutor)
        assert ex.get_account_snapshot().base_totals == {"BTC": D("3.00000")}

        # the liquidation lands on the executor and persists; the wiring (which would
        # flip the session row) never runs — the crash window
        liq_ts = h._next[session_id] + timedelta(hours=1)
        fills = await ex.on_closed_bar(
            "BTCUSDT",
            OHLCV(D("100"), D("100"), D("60"), D("60"), D("10"), liq_ts),
        )
        assert [f.order_type for f in fills] == ["LIQUIDATION"]
        assert ex.get_account_snapshot().halted is not None
        async with pool.acquire() as conn:
            stored = await paper_store.load(conn, session_id)
            orphaned = await paper_store.has_open_positions(conn, session_id)
            [liq_row] = await conn.fetch(
                "SELECT net_pnl FROM trades WHERE session_id = $1 AND order_type = 'LIQUIDATION'",
                session_id,
            )
            row = await conn.fetchrow(
                "SELECT status FROM trading_sessions WHERE id = $1", session_id
            )
        assert stored is not None
        assert stored.state["halted"] is not None and "liquidated" in stored.state["halted"]
        assert stored.state["positions"]["BTCUSDT"]["qty"] == "0"
        assert orphaned is False
        assert liq_row["net_pnl"] == D("-100.100")
        assert row is not None and row["status"] == "running"  # died before the row flipped
        await first.graceful_shutdown()  # rows stay running for recovery

        app = FastAPI()
        app.state.db_pool = pool
        await lifespan._init_session_manager(app, HydraConfig(), _clients(h))
        second: SessionManager = app.state.session_manager
        await settle()
        try:
            resumed = second.get_session(session_id)
            assert resumed is not None
            # never resumed: the liquidated session restores halted and is marked error
            assert resumed.status == "error" and not resumed.task_alive
            assert resumed.error_message is not None and "liquidated" in resumed.error_message
            ex2 = resumed._executor
            assert isinstance(ex2, PerpPaperTradingExecutor)
            snap2 = ex2.get_account_snapshot()
            assert snap2.halted is not None and snap2.liquidations == 1
            assert (await ex2.get_positions()) == []
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status, error_message FROM trading_sessions WHERE id = $1",
                    session_id,
                )
                open_rows = await paper_store.has_open_positions(conn, session_id)
            assert row is not None and row["status"] == "error"
            assert "liquidated" in row["error_message"]
            assert open_rows is False
        finally:
            await second.graceful_shutdown()
    finally:
        await pool.close()


async def test_a_liquidated_perp_session_persists_and_restores_halted(
    fresh_database: str, strategies_dir: Path
) -> None:
    await asyncio.to_thread(upgrade_to_head, fresh_database)
    await perp_liquidation_restart_scenario(fresh_database, strategies_dir)
