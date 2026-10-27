"""The live reconcile against the fake exchange: report, never adopt, never rebase.

Every pass asserts the state machine exactly: the clean pass (``ok``, no detail), the R5
no-adopt rule (an untracked exchange order is a mismatch finding, never a row, never
touched by flatten), the proven-terminal apply through the store's monotonic
``apply_status``, the delegation of every unsettled row to A1's own resolution path
(the poller's sweep — a landed unknown resolves, a three-strike un-found rejects), the
claim-apply-confirm replay lock, the fail-closed ``unknown`` on any failed read, the
balance drift that is reported while the ledger is never auto-corrected, and R20's
``covered_at`` (an ``unknown`` pass verifies nothing).
"""

from __future__ import annotations

import contextlib
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from prometheus_client import REGISTRY

from hydra.core.config import HydraConfig
from hydra.core.event_bus import InMemoryEventBus
from hydra.execution.errors import SubmitUnknownError
from hydra.execution.live_executor import LiveOrderPoller, LiveTradingExecutor
from hydra.execution.live_orders import InMemoryLiveOrderStore, LiveOrderStatus
from hydra.execution.order_manager import OrderManager
from hydra.execution.reconcile import ReconcileStatus, reconcile_live_session
from hydra.execution.session_manager import SessionManager, TradingSession
from hydra.risk.market_rules import MarketRules
from tests.fixtures.binance_trading import FakeBinanceTradingClient

D = Decimal
T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)

RULES = MarketRules(
    symbol="BTCUSDT",
    amount_step=D("0.00001"),
    min_qty=D("0.00001"),
    min_notional=D("5"),
    price_tick=D("0.01"),
    trailing_delta_bips=(D("10"), D("2000")),
)


def _executor(
    fake: FakeBinanceTradingClient,
    *,
    initial: str = "10000",
    session: str = "sess",
) -> LiveTradingExecutor:
    ex = LiveTradingExecutor(
        fake,
        exchange_id="binance",
        initial_balances={"USDT": D(initial)},
        db_pool=None,
        strategy_id="strat",
        session_id=session,
        market_rules={"BTCUSDT": RULES},
        poll_interval=5.0,
        resolve_grace=0.0,
    )
    ex.set_mark("BTCUSDT", D("100000"))
    return ex


def _poller(ex: LiveTradingExecutor) -> LiveOrderPoller:
    om = OrderManager(ex, InMemoryEventBus(), session_id=ex._session_id)
    return LiveOrderPoller(
        ex, om, interval=0.05, resolve_grace=ex.resolve_grace, not_found_window=0.0
    )


async def _reconcile(
    ex: LiveTradingExecutor,
    fake: FakeBinanceTradingClient,
    *,
    poller: LiveOrderPoller | None = None,
    previous: ReconcileStatus | None = None,
    at: datetime | None = None,
) -> ReconcileStatus:
    return await reconcile_live_session(
        ex.live_orders,
        fake,
        ex,
        poller=_poller(ex) if poller is None else poller,
        previous=previous,
        now=at,
    )


async def _buy_filled(ex: LiveTradingExecutor, qty: str = "0.01000") -> None:
    """A market buy whose fill the poller applied: the position is in the ledger."""
    await ex.create_order("BTCUSDT", "BUY", "MARKET", D(qty), params={"tag": "entry"})
    await _poller(ex).sweep()


# ---------------------------------------------------------------------------
# The state machine
# ---------------------------------------------------------------------------


async def test_the_clean_pass_is_ok_with_no_detail() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "ok"
    assert status.detail is None
    assert status.last_reconciled_at == T0
    assert status.covered_at == T0


async def test_an_untracked_exchange_order_is_a_mismatch_and_is_never_adopted() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    before = await ex.live_orders.orders("sess", 100)
    # the user's own order on the session's symbol, placed outside hydra
    await fake.create_order("BTCUSDT", "BUY", "LIMIT", D("0.01"), D("90000"), None, {})

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "mismatch"
    assert "untracked order 101 on BTCUSDT (not managed)" in status.detail
    # never adopted: the session's rows are exactly its own
    assert await ex.live_orders.orders("sess", 100) == before
    # never traded by flatten: the user's order is still resting after a flatten
    await ex.flatten()
    assert [o["clientOrderId"] for o in fake.open_orders_raw()] == ["auto-101"]


async def test_another_sessions_tracked_order_is_tracked_elsewhere_never_untracked() -> None:
    """A1 (R29): the untracked classification is account-wide — one live_orders table."""
    fake = FakeBinanceTradingClient(balances={"USDT": D("20000")})
    shared = InMemoryLiveOrderStore()  # one live_orders table, two sessions on one account
    a = _executor(fake, session="sess-a")
    b = _executor(fake, session="sess-b")
    a._live_orders = shared
    b._live_orders = shared
    await _buy_filled(a)  # a trades BTCUSDT, so its pass reads the symbol's open orders
    await b.create_order("BTCUSDT", "BUY", "LIMIT", D("0.01"), D("90000"), params={"tag": "entry"})

    status = await _reconcile(a, fake, at=T0)

    assert status.state == "ok"  # b's tracked order is tracked-elsewhere, never 'untracked'
    assert status.detail is None
    assert (await _reconcile(b, fake, at=T0)).state == "ok"


async def test_a_row_open_locally_but_gone_on_the_exchange_is_terminalized_from_proof() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await ex.create_order("BTCUSDT", "BUY", "LIMIT", D("0.01"), D("90000"), params={"tag": "entry"})
    (row,) = await ex.live_orders.open_orders("sess")
    await fake.cancel_order(row.exchange_order_id or "", "BTCUSDT")  # gone unseen

    # no poller: this pass is the one that must prove and apply the terminal itself
    status = await reconcile_live_session(ex.live_orders, fake, ex, now=T0)

    assert status.state == "mismatch"
    assert f"order {row.client_order_id} canceled on the exchange (status applied)" in status.detail
    gone = await ex.live_orders.by_client_order_id(row.client_order_id)
    assert gone is not None and gone.status == LiveOrderStatus.CANCELED


async def test_a_row_the_poller_already_refreshed_is_not_a_reconcile_finding() -> None:
    """The poller's own refresh applied the terminal seconds before the pass: the world
    is consistent at pass time, so the pass reports ok."""
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await ex.create_order("BTCUSDT", "BUY", "LIMIT", D("0.01"), D("90000"), params={"tag": "entry"})
    (row,) = await ex.live_orders.open_orders("sess")
    await fake.cancel_order(row.exchange_order_id or "", "BTCUSDT")

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "ok"
    gone = await ex.live_orders.by_client_order_id(row.client_order_id)
    assert gone is not None and gone.status == LiveOrderStatus.CANCELED


# ---------------------------------------------------------------------------
# (d): the delegation to A1's resolution path
# ---------------------------------------------------------------------------


async def test_a_landed_unknown_resolves_through_the_delegated_path() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    fake.fail_create = "timeout_after_land"
    with contextlib.suppress(SubmitUnknownError):
        await _buy_filled(ex)

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "ok"
    (row,) = await ex.live_orders.orders("sess", 100)
    assert row.status == LiveOrderStatus.FILLED
    assert row.exchange_order_id == "100"
    assert await ex.fetch_balance() == {
        "USDT": D("9000"),
        "BTC": D("0.00999"),
    }


async def test_a_three_strike_unfound_submit_is_rejected_by_the_delegated_path() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    fake.fail_create = "timeout_not_landed"
    with contextlib.suppress(SubmitUnknownError):
        await _buy_filled(ex)
    (row,) = await ex.live_orders.orders("sess", 100)

    poller = _poller(ex)  # the runtime's one poller: the window state lives on it
    first = await _reconcile(ex, fake, poller=poller)
    assert first.state == "mismatch"
    assert f"unresolved submit {row.client_order_id}" in first.detail

    second = await _reconcile(ex, fake, poller=poller)
    assert second.state == "mismatch"
    assert f"unresolved submit {row.client_order_id}" in second.detail

    third = await _reconcile(ex, fake, poller=poller)
    assert third.state == "ok"  # A1's window rejected it; nothing is left to report
    gone = await ex.live_orders.by_client_order_id(row.client_order_id)
    assert gone is not None and gone.status == LiveOrderStatus.REJECTED


# ---------------------------------------------------------------------------
# (e): the fill catch-up rides the claim-apply-confirm pipeline
# ---------------------------------------------------------------------------


async def test_the_fill_catch_up_is_idempotent_across_passes() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    resp = await ex.create_order("BTCUSDT", "BUY", "LIMIT", D("0.01"), D("90000"))
    await fake.fill_order(int(resp["id"]), "90000")  # filled while nobody was polling

    first = await _reconcile(ex, fake, at=T0)
    assert first.state == "ok"
    after_first = await ex.fetch_balance()
    assert after_first == {"USDT": D("9100"), "BTC": D("0.00999")}

    second = await _reconcile(ex, fake, at=T0 + timedelta(minutes=1))
    assert second.state == "ok"
    assert await ex.fetch_balance() == after_first  # nothing is applied twice
    assert await ex.live_orders.unapplied_fills("sess") == []


# ---------------------------------------------------------------------------
# (f): the balance drift
# ---------------------------------------------------------------------------


async def test_balance_drift_is_a_mismatch_and_never_autocorrects_the_ledger() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    fake.balances["USDT"] = D("100")  # the wallet lost the session's quote

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "mismatch"
    assert "balance drift USDT local 9000 exchange 100" in status.detail
    assert (await ex.fetch_balance())["USDT"] == D("9000")  # the ledger is untouched


async def test_resting_locked_funds_stay_in_the_wallet_total_and_are_not_drift() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    # a resting protective SELL stop locks the session's BTC: free drops, total keeps it
    await ex.create_order("BTCUSDT", "SELL", "STOP_MARKET", D("0.00999"), stop_price=D("95000"))
    fake.locked["BTC"] = D("0.00999")
    assert await fake.fetch_balance() == {"USDT": D("9000")}  # the free BTC is fully locked

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "ok"  # the wallet total still holds the locked BTC


async def test_a_user_order_locking_shared_wallet_funds_is_not_drift() -> None:
    """A2: the compare is total-based — the user's resting BUY locks quote, total keeps it."""
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    # the user's own ETHUSDT BUY rests and locks 8000 USDT of the shared wallet
    await fake.create_order("ETHUSDT", "BUY", "LIMIT", D("8"), D("1000"), None, {})
    assert await fake.fetch_balance() == {"USDT": D("1000"), "BTC": D("0.00999")}

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "ok"  # the wallet total still holds the sleeve's 9000 USDT


async def test_a_partially_filled_resting_sell_cannot_mask_a_btc_hole() -> None:
    """A3: 0.007 BTC left the wallet while myTrades was unreadable — the total catches it."""
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)  # the sleeve holds 0.00999 BTC
    resp = await ex.create_order("BTCUSDT", "SELL", "LIMIT", D("0.00999"), D("110000"))
    await fake.fill_order(int(resp["id"]), "110000", "0.007")  # partially filled, unseen

    async def no_trades(symbol: str, since: Any = None) -> list[dict[str, Any]]:
        raise RuntimeError("myTrades unreadable")

    fake.fetch_my_trades = no_trades  # the fill cannot reach the ledger this pass
    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "mismatch"
    assert "balance drift BTC local 0.00999 exchange 0.00299" in status.detail


async def test_a_wallet_surplus_over_the_sleeve_is_not_drift() -> None:
    fake = FakeBinanceTradingClient(balances={"USDT": D("50000")})
    ex = _executor(fake, initial="10000")  # a shared wallet: Q3 tracks the sleeve only
    await _buy_filled(ex)

    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "ok"


# ---------------------------------------------------------------------------
# The fail-closed vocabulary
# ---------------------------------------------------------------------------


async def test_a_failed_exchange_read_is_unknown_never_ok_or_mismatch() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)

    async def boom(symbol: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("exchange down")

    fake.fetch_open_orders = boom
    status = await _reconcile(ex, fake, at=T0)

    assert status.state == "unknown"
    assert "open orders of BTCUSDT unreadable" in status.detail
    assert status.state not in ("ok", "mismatch")


async def test_an_unknown_pass_verifies_nothing_r20_covered_at() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    ok = await _reconcile(ex, fake, at=T0)
    assert ok.covered_at == T0

    async def boom(symbol: str | None = None) -> list[dict[str, Any]]:
        raise RuntimeError("exchange down")

    fake.fetch_open_orders = boom
    unknown = await _reconcile(ex, fake, previous=ok, at=T0 + timedelta(minutes=1))

    assert unknown.state == "unknown"
    assert unknown.last_reconciled_at == T0 + timedelta(minutes=1)
    assert unknown.covered_at == T0  # the failed pass covers no fill


# ---------------------------------------------------------------------------
# The holder: the manager's reconcile status and the loop
# ---------------------------------------------------------------------------


def _live_session(session_id: str, executor: Any) -> TradingSession:
    session = TradingSession(
        session_id=session_id,
        strategy_id="strat",
        trading_mode="live",
        status="running",
        exchange_id="binance",
        symbols=["BTCUSDT"],
        timeframe="1h",
        paper_capital=D("10000"),
    )
    session._executor = executor
    return session


async def test_a_live_session_reports_never_before_the_first_pass() -> None:
    mgr = SessionManager(config=HydraConfig())
    mgr._sessions["s1"] = _live_session("s1", None)
    mgr._sessions["s2"] = TradingSession(
        session_id="s2",
        strategy_id="strat",
        trading_mode="paper",
        status="running",
    )

    assert mgr.reconcile_status("s1") == ReconcileStatus(state="never")
    assert mgr.reconcile_status("s2") is None  # paper carries no reconcile state
    health = await mgr.health_snapshot()
    live = next(h for h in health.sessions if h.session_id == "s1")
    paper = next(h for h in health.sessions if h.session_id == "s2")
    assert (live.reconcile_state, live.last_reconciled_at) == ("never", None)
    assert (paper.reconcile_state, paper.last_reconciled_at) == (None, None)


async def test_the_loop_reconciles_every_running_live_session_and_holds_the_status() -> None:
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    mgr = SessionManager(config=HydraConfig())
    mgr._sessions["s1"] = _live_session("s1", ex)

    await mgr._reconcile_live_sessions()

    status = mgr.reconcile_status("s1")
    assert status is not None and status.state == "ok"
    health = await mgr.health_snapshot()
    live = next(h for h in health.sessions if h.session_id == "s1")
    assert live.reconcile_state == "ok"
    assert live.last_reconciled_at is not None

    # the cadence gate: a second call inside live_reconcile_interval_s runs no pass
    held = mgr._reconcile["s1"]
    await mgr._reconcile_live_sessions()
    assert mgr._reconcile["s1"] is held
    assert time.monotonic() - (mgr._reconcile_last or 0.0) < 3600


async def test_a_failed_store_read_mid_pass_is_unknown_never_a_latched_ok() -> None:
    """A4≡S5: a dependent read failing mid-pass updates the holder and the gauge."""
    fake = FakeBinanceTradingClient()
    ex = _executor(fake)
    await _buy_filled(ex)
    mgr = SessionManager(config=HydraConfig())
    mgr._sessions["s1"] = _live_session("s1", ex)

    await mgr._reconcile_live_sessions()
    assert mgr.reconcile_status("s1").state == "ok"
    assert REGISTRY.get_sample_value("hydra_live_reconcile_state", {"session_id": "s1"}) == 0.0

    async def boom(session_id: str) -> list[Any]:
        raise RuntimeError("database down")

    ex.live_orders.open_orders = boom  # a pass-only store read fails mid-pass
    mgr._reconcile_last = None  # the cadence gate would otherwise skip the tick

    await mgr._reconcile_live_sessions()

    status = mgr.reconcile_status("s1")
    assert status is not None and status.state == "unknown"
    assert "dependent read failed: database down" in (status.detail or "")
    assert REGISTRY.get_sample_value("hydra_live_reconcile_state", {"session_id": "s1"}) == 2.0


async def test_a_running_live_session_without_a_completed_pass_exports_never() -> None:
    """A7≡S9: 'never' is a real gauge export, not a missing series."""
    mgr = SessionManager(config=HydraConfig())
    mgr._sessions["s-never"] = _live_session("s-never", None)  # no executor: no pass runs

    await mgr._reconcile_live_sessions()

    status = mgr.reconcile_status("s-never")
    assert status is not None and status.state == "never"
    assert REGISTRY.get_sample_value("hydra_live_reconcile_state", {"session_id": "s-never"}) == 3.0
