"""THE RESTORE TEST on real TimescaleDB: a live session resumes after a restart.

Scenario: a live session holds BTC and ETH with protective stops resting on the (fake)
exchange. While the engine is down the BTC stop fills, an ETH limit order from a crashed
submit stays unresolved, and the ETH position keeps its stop. A second SessionManager must
resume the session (no zombie), apply the fill that happened while down exactly once,
re-adopt the protective group from the tracked rows, and resolve the unknown submit by its
client order id. A second live row with an unrestorable ledger is marked error, not resumed.
Only the ccxt boundary is fake; everything above it — the manager, the risk services, the
ledger persistence — is real.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import asyncpg
import pytest
from cryptography.fernet import Fernet

from hydra.core.config import HydraConfig
from hydra.data.migrations.runner import upgrade_to_head
from hydra.execution.client_factory import ClientFactory, bars_repository
from hydra.execution.session_manager import SessionManager
from tests.fixtures.binance_trading import FakeBinanceTradingClient
from tests.unit.test_execution.session_harness import (
    Harness,
    rule,
    write_strategy,
)

pytestmark = pytest.mark.integration

D = Decimal
LIVE_CAPITAL = D("5000")
POLL_INTERVAL_S = 1  # HYDRA_TRADING__LIVE_POLL_INTERVAL_S for the test's managers


async def _seed(pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HYDRA_CREDENTIAL_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("HYDRA_TRADING__LIVE_POLL_INTERVAL_S", str(POLL_INTERVAL_S))
    from hydra.core.encryption import encrypt

    async with pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO system_config (key, value) VALUES ($1, $2::jsonb)",
            "live_capital",
            str(int(LIVE_CAPITAL)),
        )
        await conn.execute(
            "INSERT INTO exchange_credentials"
            " (exchange_id, encrypted_key, encrypted_secret, updated_at, validated_at,"
            "  validated_environment)"
            " VALUES ('binance', $1, $2, now(), now(), 'testnet')",
            encrypt("test-key"),
            encrypt("test-secret"),
        )


def _fake_clients(h: Harness, fake: FakeBinanceTradingClient) -> ClientFactory:
    base = h.clients()
    return ClientFactory(
        ws_factory=base.ws_factory,
        rest_pool=base.rest_pool,
        markets_loader=base.markets_loader,
        repository_factory=bars_repository,
        history_window=base.history_window,
        trading_client_factory=lambda eid, creds, testnet: fake,
    )


def _manager(h: Harness, pool: asyncpg.Pool, fake: FakeBinanceTradingClient) -> SessionManager:
    return SessionManager(db_pool=pool, config=HydraConfig(), clients=_fake_clients(h, fake))


async def _live_entry(
    h: Harness, pool: asyncpg.Pool, fake: FakeBinanceTradingClient, sid: str
) -> SessionManager:
    """Start the live session and drive one entry bar: positions in BTC and ETH."""
    mgr = _manager(h, pool, fake)
    await mgr.startup()
    session_id = await mgr.start_session(sid, "live")
    session = mgr.get_session(session_id)
    assert session is not None and session.trading_mode == "live"
    fake.set_mark("BTCUSDT", "100")
    fake.set_mark("ETHUSDT", "110")
    await h.bar(session, "100", symbol="BTCUSDT")
    await h.bar(session, "110", symbol="ETHUSDT")
    # one poll cycle applies the fills and auto-arms the protective stops
    await asyncio.sleep(POLL_INTERVAL_S + 0.4)
    for symbol in ("BTCUSDT", "ETHUSDT"):
        assert any(p.quantity > 0 for p in await session._executor.get_positions(symbol)), symbol
        assert fake.open_orders_raw(symbol), f"no protective stop resting on {symbol}"
    return mgr


async def _order_ids_on(fake: FakeBinanceTradingClient, symbol: str, order_type: str) -> list[int]:
    return [
        o["orderId"]
        for o in fake.orders.values()
        if o["symbol"] == symbol and o["type"] == order_type
    ]


async def test_live_session_resumes_and_reconciles_all_three_cases(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import infra

    db = await infra.create_database()
    dsn = infra.dsn(db)
    try:
        await asyncio.to_thread(upgrade_to_head, dsn)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        try:
            await _seed(pool, monkeypatch)
            h = Harness(strategies_dir)
            sid = write_strategy(
                strategies_dir,
                "pg-live",
                entry_long=rule(("less_than", 1000)),
                symbols=("BTCUSDT", "ETHUSDT"),
            )
            fake = FakeBinanceTradingClient(balances={"USDT": D("20000")})
            first = await _live_entry(h, pool, fake, sid)
            session = first.list_sessions()[0]
            assert first.live_risk is not None
            assert session.session_id in first.live_risk.registered_sessions()

            # (b) the BTC protective stop fills while the engine is down
            btc_stop = (await _order_ids_on(fake, "BTCUSDT", "STOP_LOSS"))[0]
            await fake.fill_order(btc_stop, price="95")
            # (c) a submit that timed out after landing: the row is unknown, the order rests
            await fake.create_order(
                "ETHUSDT",
                "SELL",
                "limit",
                D("0.10"),
                price=D("120"),
                params={"newClientOrderId": "hyd-unresolved-1"},
            )
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO live_orders"
                    " (client_order_id, session_id, strategy_id, exchange_id, symbol, side,"
                    "  order_type, quantity, status, tag, submitted_at)"
                    " VALUES ('hyd-unresolved-1', $1, $2, 'binance', 'ETHUSDT', 'SELL',"
                    "  'LIMIT', 0.10, 'unknown', 'exit', now() - interval '5 minutes')",
                    session.session_id,
                    sid,
                )
            # (a) the ETH position and its protective stop keep resting
            eth_stop = (await _order_ids_on(fake, "ETHUSDT", "STOP_LOSS"))[0]
            assert fake.orders[eth_stop]["status"] == "NEW"

            await first.graceful_shutdown()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT status FROM trading_sessions WHERE id = $1", session.session_id
                )
            assert row["status"] == "running"  # the row survives for recovery

            second = _manager(h, pool, fake)
            await second.startup()
            try:
                resumed = second.get_session(session.session_id)
                assert resumed is not None
                assert resumed.status == "running" and resumed.task_alive
                assert resumed._runtime is not None
                await asyncio.sleep(0.3)  # the poller's first sweep ran at wire()

                # (b) the fill that happened while down applied exactly once
                async with pool.acquire() as conn:
                    btc_fills = await conn.fetchval(
                        "SELECT count(*) FROM live_fills WHERE symbol = 'BTCUSDT' AND side = 'SELL'"
                    )
                    btc_trades = await conn.fetchval(
                        "SELECT count(*) FROM trades WHERE session_id = $1"
                        " AND symbol = 'BTCUSDT' AND side = 'SELL'",
                        session.session_id,
                    )
                assert (btc_fills, btc_trades) == (1, 1)
                assert not any(
                    p.quantity > 0 for p in await resumed._executor.get_positions("BTCUSDT")
                )

                # (a) the ETH position survives and its protective group was re-adopted
                assert any(p.quantity > 0 for p in await resumed._executor.get_positions("ETHUSDT"))
                assert resumed._runtime.protect.armed("ETHUSDT") is not None
                assert fake.orders[eth_stop]["status"] == "NEW"

                # (c) the unknown submit resolved to its resting exchange order
                async with pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT status, exchange_order_id FROM live_orders"
                        " WHERE client_order_id = 'hyd-unresolved-1'"
                    )
                assert row["status"] == "new"
                assert row["exchange_order_id"] is not None

                # the live risk service carried the sleeve through the restart
                assert second.live_risk is not None
                assert session.session_id in second.live_risk.registered_sessions()
                # no zombie: the task is alive and the row agrees
                assert resumed._task is not None and not resumed._task.done()
            finally:
                await second.graceful_shutdown()
        finally:
            await pool.close()
    finally:
        await infra.drop_database(db)


async def test_an_unrestorable_live_ledger_marks_the_session_error(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests import infra

    db = await infra.create_database()
    dsn = infra.dsn(db)
    try:
        await asyncio.to_thread(upgrade_to_head, dsn)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        try:
            await _seed(pool, monkeypatch)
            h = Harness(strategies_dir)
            sid = write_strategy(strategies_dir, "pg-live-broken")
            async with pool.acquire() as conn:
                await conn.execute(
                    "INSERT INTO trading_sessions"
                    " (id, strategy_id, trading_mode, status, exchange_id, symbols, timeframe,"
                    "  paper_capital)"
                    " VALUES ('live-broken', $1, 'live', 'running', 'binance',"
                    "  ARRAY['BTCUSDT'], '1h', 1000)",
                    sid,
                )
                await conn.execute(
                    "INSERT INTO paper_accounts"
                    " (session_id, strategy_id, exchange_id, quote_asset, state, state_version)"
                    " VALUES ('live-broken', $1, 'binance', 'USDT',"
                    "  '{\"format\": 99}'::jsonb, 1)",
                    sid,
                )

            mgr = _manager(h, pool, FakeBinanceTradingClient())
            await mgr.startup()
            try:
                broken = mgr.get_session("live-broken")
                assert broken is not None
                assert broken.status == "error"
                assert broken.error_message is not None
                assert "recovery failed" in broken.error_message
                assert not broken.task_alive
            finally:
                await mgr.graceful_shutdown()
        finally:
            await pool.close()
    finally:
        await infra.drop_database(db)


async def test_live_capital_is_divided_between_running_live_sessions(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F6: a live start seeds the REMAINDER of the armed budget, never the full
    live_capital again, and refuses when nothing is left (paper's _allocate model)."""
    from tests import infra

    db = await infra.create_database()
    dsn = infra.dsn(db)
    try:
        await asyncio.to_thread(upgrade_to_head, dsn)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        try:
            await _seed(pool, monkeypatch)
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE system_config SET value = '10000'::jsonb WHERE key = 'live_capital'"
                )
            h = Harness(strategies_dir)
            fake = FakeBinanceTradingClient(balances={"USDT": D("4000")})
            mgr = _manager(h, pool, fake)
            await mgr.startup()
            try:
                seeds = []
                for i in range(3):
                    sid = write_strategy(strategies_dir, f"pg-live-cap-{i}")
                    session_id = await mgr.start_session(sid, "live")
                    session = mgr.get_session(session_id)
                    assert session is not None
                    seeds.append(session.paper_capital)
                assert seeds == [D("4000"), D("4000"), D("2000")]  # min(free, remainder)
                with pytest.raises(ValueError, match="Insufficient live capital"):
                    await mgr.start_session(write_strategy(strategies_dir, "pg-live-cap-3"), "live")
            finally:
                await mgr.graceful_shutdown()
        finally:
            await pool.close()
    finally:
        await infra.drop_database(db)


async def test_two_live_sessions_on_one_account_book_only_their_own_fills(
    strategies_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1/F13's cross-session contract on real PG: one live_orders/live_fills table, one
    Binance account, two sessions — each poller claims only its session's trades, so the
    account-wide unique key never blocks the other session's fill."""
    from tests import infra

    db = await infra.create_database()
    dsn = infra.dsn(db)
    try:
        await asyncio.to_thread(upgrade_to_head, dsn)
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=6)
        try:
            await _seed(pool, monkeypatch)
            async with pool.acquire() as conn:
                await conn.execute(
                    "UPDATE system_config SET value = '20000'::jsonb WHERE key = 'live_capital'"
                )
            h = Harness(strategies_dir)
            fake = FakeBinanceTradingClient(balances={"USDT": D("9000")})
            mgr = _manager(h, pool, fake)
            await mgr.startup()
            try:
                sessions = []
                for name in ("pg-live-x", "pg-live-y"):
                    sid = write_strategy(strategies_dir, name, entry_long=rule(("less_than", 1000)))
                    session_id = await mgr.start_session(sid, "live")
                    session = mgr.get_session(session_id)
                    assert session is not None
                    sessions.append(session)
                fake.set_mark("BTCUSDT", "100")
                for session in sessions:
                    await h.bar(session, "100")
                # both pollers sweep on their own 1 s cadence, in any order
                await asyncio.sleep(POLL_INTERVAL_S + 0.6)
                await asyncio.sleep(POLL_INTERVAL_S + 0.6)

                for session in sessions:
                    executor = session._executor
                    assert executor is not None
                    positions = [
                        p.quantity
                        for p in await executor.get_positions("BTCUSDT")
                        if p.quantity > 0
                    ]
                    assert positions, f"{session.session_id} booked no entry fill"
                    async with pool.acquire() as conn:
                        rows = await conn.fetch(
                            "SELECT f.client_order_id, f.session_id FROM live_fills f"
                            " JOIN live_orders o ON o.client_order_id = f.client_order_id"
                            " WHERE f.session_id = $1",
                            session.session_id,
                        )
                        own = {
                            r["client_order_id"]
                            for r in rows
                            if r["session_id"] == session.session_id
                        }
                    assert rows and own, f"{session.session_id} claimed no fill of its own"
                    # every fill row this session claimed belongs to one of ITS orders
                    async with pool.acquire() as conn:
                        others = await conn.fetchval(
                            "SELECT count(*) FROM live_fills f"
                            " JOIN live_orders o ON o.client_order_id = f.client_order_id"
                            " WHERE f.session_id = $1 AND o.session_id <> $1",
                            session.session_id,
                        )
                    assert others == 0, "a fill of another session's order was claimed"
                async with pool.acquire() as conn:
                    total = await conn.fetchval("SELECT count(*) FROM live_fills")
                assert total == 2  # exactly one entry fill per session, nothing hijacked
            finally:
                await mgr.graceful_shutdown()
        finally:
            await pool.close()
    finally:
        await infra.drop_database(db)
