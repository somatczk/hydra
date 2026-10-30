# src/hydra/services/

Transport-neutral operations that the REST routes and the MCP tools both call, plus the live gate and the
approval store. Repo-wide rules are in the root `AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_services -q
uv run pytest tests/unit/test_dashboard/test_live_routes.py tests/unit/test_dashboard/test_activity_routes.py tests/unit/test_dashboard/test_mcp_tools.py -q
uv run mypy src/
uv run mypy src/hydra/services/ --strict   # the package is on CI's strict line (no relaxation)
```

## Invariants

- No `fastapi`, `starlette` or `hydra.dashboard` import anywhere in this package
  (`tests/unit/test_services/test_no_http.py`). Business logic lives here; routes and tools keep only their
  request models, principals and audit lines.
- Fail with `ServiceError` built by `service_error(...)` (`errors.py`): the same `{"code", "message",
  "state", ...}` detail the routes answer and MCP tools return. Wrap DB calls in `db_errors(...)` so
  connection failures are 503 `database_unavailable` and real bugs stay 500.
- `trader.py`'s `TraderBackend` is the only way into the engine; its op set and signatures are pinned and
  `LocalTrader`/`RemoteTrader` implement exactly it (`tests/unit/test_services/test_trader_backend.py`).
  A new op changes the protocol, both implementations, the trader's internal API and the pinned count together.
- `sessions.py` is the session, kill-switch and quick-trade seam; the actor string (`user:<name>` or
  `service:mcp`) is what attributes `risk_events` rows, `trading_sessions.created_by` and audit lines.
- `approvals.py` (`ApprovalStore`, one per process, in memory, nothing survives a restart): at most 20
  pending, 6-digit `secrets` code, 10-minute expiry, 5 wrong attempts invalidate, a second request for
  the same action returns the pending one; every transition is a `hydra.audit` line plus a best-effort
  `risk_events` `approval_*` row. Executors run only after the code matches, must re-validate their target
  state and stay idempotent; a `ServiceError` they raise reaches the caller unchanged (R22), anything else
  is 503 `approval_execution_failed` (`tests/unit/test_services/test_approvals.py`). Service principals
  cannot approve or deny (`tests/unit/test_dashboard/test_activity_routes.py::TestApprove::test_service_principal_cannot_approve`).
- `live_gate.py`: the `live_armed` row is written ON only by the `live_arming` executor and OFF also by the
  ungated disarm; an absent or unreadable row reads disarmed. Live executors registered on the store:
  `live_arming`, `live_session_start`, `live_order`, `credential_delete` (`LIVE_ACTIONS`), plus
  `live_perps_arming` from `perps_gate.py`
  (`tests/unit/test_services/test_live_gate.py::test_the_live_actions_are_the_four_executors`). A different
  pending request of the same live action is 409 `approval_conflict`. Arming re-fetches the key's Binance
  API restrictions live (R8) and refuses unless mainnet, spot-trade-enabled and withdrawal-disabled.
- Every other surface refuses live with 409 `live_gate_required` (`live_gate_required()`; the trading
  routes, the quick-trade MCP tool, the webhook: `tests/unit/test_services/test_sessions.py::test_webhook_refuses_a_live_target_row_before_any_order`).
- `perps_gate.py`: `perps_armed`/`perps_capital` rows, separate from spot; arming spot never arms perps.
  The arm re-fetches `enableFutures` and refuses on any failure or absent flag. Every live FUTURES surface
  refuses 409 `perps_not_armed`, or 403 `perps_capital_missing` when armed without capital
  (`tests/unit/test_services/test_perps_gate.py`).

## Where things are

- `sessions.py`, `trader.py`, `approvals.py`, `live_gate.py`, `perps_gate.py`, `errors.py`.
- `backtest.py`, `research.py`, `strategies.py`, `data.py`, `universe.py`, `models.py`: research, strategy,
  data and model-registry operations.
- Other code-gated executors registered on the same store: `kill_switch_release`
  (`dashboard/mcp/tools/risk.py`), `promote_model`/`rollback_model` (`dashboard/mcp/tools/models.py`).
