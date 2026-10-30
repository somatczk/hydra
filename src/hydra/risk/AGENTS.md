# src/hydra/risk/

Pre-trade checks, circuit breakers, the kill switch, sizing and market rules. Repo-wide rules are in the
root `AGENTS.md`; the protective-stop invariants live in `src/hydra/execution/AGENTS.md`.

## Commands

```bash
uv run pytest tests/unit/test_risk -q
uv run mypy src/hydra/execution/ src/hydra/risk/ --strict   # part of CI's strict list
```

## Invariants

- One `RiskService` per process and scope: `paper`, plus `live` (capital from `system_config.live_capital`)
  when that row exists. Backtests build their own on an `InMemoryRiskStateStore` and bar time
  (drawdown tiers off).
- Orders are classified first. A `reduce_only` order (clamped to the held position by the OrderManager)
  is never blocked by the kill switch, breakers, limits, rate limit or balance; only the sanity check
  applies (`tests/unit/test_risk/test_pretrade.py::TestIntent::test_exit_never_blocked`). Opening orders run
  the full sequence in `PreTradeRiskManager.evaluate`, priced at the latest mark.
- Fail closed: if risk state cannot be loaded or the last save failed, openings are refused and exits pass
  (`tests/unit/test_risk/test_risk_service.py::TestFailClosed`). After a restart openings stay refused until
  every running session re-registers and `recovery_complete()` runs.
- Breakers: four drawdown tiers at 1/5, 1/3, 2/3 and 1x of `max_drawdown_pct` (size x0.5, no entries,
  flatten all, kill switch); tiers 1-2 auto-reset after 24 h / 48 h, tiers 3-4 are manual only
  (`tests/unit/test_risk/test_circuit_breakers.py`). Daily-loss halt latches until 00:00 UTC.
- Persistence: breaker state in `risk_state` (per scope), the kill-switch flag in
  `risk_config.kill_switch_active`, and every transition, reset and kill-switch change as a `risk_events`
  row plus a `hydra.audit` line (`state.py`).
- A `paper_capital` change goes through `RiskService.reload_config`: an audited rebase of the ledger that
  keeps the breaker tier (`tests/unit/test_risk/test_risk_service.py::TestPaperReset`).
- Sizes are `Decimal` base units (`sizing.py`); market rules come from public ccxt metadata, no keys
  (`market_rules.py`), for the registered venues only — binance, binanceusdm and bybit (the registry's
  venue ids). Perp trailing bounds are the fapi `callbackRate` percent, not spot's bips; bybit has no
  percent trail at all (the registry's trailing kind NONE), so its rules carry no trailing bounds and the
  protective ladder falls back to the fixed stop.

## Where things are

- `service.py`: `RiskService` (equity ledger, breakers, kill switch, `check`, `size_entry`).
- `pretrade.py`: intent classification and the ordered opening checks.
- `circuit_breakers.py`, `state.py`, `sizing.py`, `market_rules.py`.
