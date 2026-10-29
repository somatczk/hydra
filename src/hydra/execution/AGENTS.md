# src/hydra/execution/

Sessions, the OrderManager, the paper/perp simulators and the live spot/perp executors. Real money moves
here; repo-wide rules are in the root `AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_execution tests/unit/test_backtest/test_execution_parity.py -q
uv run mypy src/hydra/execution/ src/hydra/risk/ --strict   # part of CI's strict list
uv run pytest tests/integration/test_session_recovery_pg.py -m integration   # needs HYDRA_DB_*
```

## Invariants

- `client_factory` is the only place that builds exchange clients, executors and feed clients on the
  session path; `ALLOWED_VENUES` is binance SPOT and binanceusdm FUTURES, anything else is
  `UnsupportedVenueError` (`tests/unit/test_execution/test_session_split.py::test_only_client_factory_constructs_exchange_clients`,
  `tests/unit/test_execution/test_client_factory.py`).
- Backtest and paper share one execution path; a change to fills or the paper executor must keep
  `tests/unit/test_backtest/test_execution_parity.py::test_backtest_equals_paper_session_on_the_same_bars` green.
  Fill rules: `fill_rules.py` (pure, Decimal, no I/O) and [docs/paper-fill-rules.md](../../../docs/paper-fill-rules.md).
- Live executors never simulate (no local fill, liquidation or funding); fills come only from the
  `LiveOrderPoller`'s REST polling at the real price and commission (`tests/unit/test_execution/test_live_no_simulation.py`).
- Live orders are outbox-first: the `pending_submit` row (a `hyd-` client id) exists before the exchange
  call, and no path resubmits an unsettled row; the poller resolves it by client id
  (`tests/unit/test_execution/test_live_orders.py::test_outbox_first_and_ack_flow`).
- No live position without a confirmed exchange-side protective order. Spot: `exchange_stops.arm_exchange_stop`
  ladder (OCO, then STOP_LOSS, then the 5 % default); a definitive rejection of every rung raises
  `StopUnplaceableError` and the position is market-closed (decision 17); a timeout propagates, never falls
  through the ladder (`tests/unit/test_execution/test_exchange_stops.py`). Operator view:
  [docs/live-trading.md](../../../docs/live-trading.md).
- Every entry fill arms one protective group (stop, OCO with a take-profit, or trailing), 5 % stop by
  default from the strategy `risk:` block (`tests/unit/test_execution/test_protective_orders.py::TestArming::test_every_entry_gets_the_default_five_percent_stop`).
- Perps have no OCO: the protective pair is two reduce-only conditional orders, stop leg first; a rejected
  leg unwinds the pair; a filled leg cancels its sibling on both the fill and the reconcile path
  (`tests/unit/test_execution/test_perp_live_executor.py`). A live perp start sets one-way position mode
  and ISOLATED margin + leverage per symbol before any order; a setup failure builds nothing
  (`tests/unit/test_execution/test_perp_live_session.py::TestLivePerpStart::test_a_venue_setup_failure_refuses_and_builds_nothing`).
- Reconcile reports, never fixes: an untracked exchange order is drift and is never adopted, balance drift
  never rewrites the ledger, a failed read is `unknown` (`tests/unit/test_execution/test_live_reconcile.py`).
  Perp reconcile is deficit-only: the exchange holding more is the user's own position
  (`tests/unit/test_execution/test_perp_live_executor.py::TestPerpReconcile::test_a_surplus_exchange_position_is_not_drift`).
- Recovery adopts only hydra's own resting orders (`hyd-` client id), never the user's
  (`tests/unit/test_execution/test_protective_orders.py`), and resumes the session's stored config snapshot, not the YAML (`tests/integration/test_session_recovery_pg.py::test_recovery_uses_snapshot`).
- SQL for `trading_sessions` lives only in `session_store.py`
  (`tests/unit/test_execution/test_session_split.py::test_sql_lives_in_session_store`).
- Every ccxt implicit method name the executors call is pinned against the installed ccxt
  (`tests/unit/test_execution/test_real_ccxt_surface.py`); add new names there.

## Where things are

- `session_manager.py` (start/stop/recovery), `session_wiring.py` (one session's runtime; the closed-bar
  handler runs before the strategy sees the bar), `order_manager.py` (dedup, clamp, risk check).
- Paper: `paper_trading.py`, `perp_trading.py`. Live (subclasses of the paper ones): `live_executor.py`,
  `perp_live.py`, plus `live_orders.py`, `reconcile.py`, `exchange_stops.py`, `protective_orders.py`.
- Exchange fakes for tests: `tests/fixtures/binance_trading.py`.
