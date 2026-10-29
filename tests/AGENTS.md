# tests

The pytest suites. The repo-wide rules in the root `AGENTS.md` apply (gates, never silence a check, fail-not-skip
for set-but-unreachable infra); this file covers how the suites are wired.

## Commands
| Suite | Needs | Command |
|-------|-------|---------|
| Unit | nothing | `uv run pytest tests/unit -q -m "not slow"` (CI adds `--cov`) |
| Integration | TimescaleDB (`HYDRA_DB_HOST`, `HYDRA_DB_PORT`, `HYDRA_DB_NAME`, `HYDRA_DB_USER`, `HYDRA_DB_PASSWORD`), Redis (`HYDRA_REDIS_URL`) | `uv run pytest tests/integration -m integration` |
| E2E | nothing, except the smokes | `uv run pytest tests/e2e -m e2e` |
| Paper + split smoke | as integration | `make smoke` |
| Performance | nothing (not run in CI) | `uv run pytest tests/performance -m performance` |
| Dashboard | Node 26 | see `dashboard/AGENTS.md` |

Run one file or test while iterating: `uv run pytest tests/unit/test_execution/test_paper_trading.py::<name> -q`.

## Invariants
- `addopts = "-ra --strict-markers"`: use only the registered markers (`unit`, `integration`, `e2e`,
  `performance`, `slow`). Tests under `tests/integration/` and `tests/e2e/` are marked by directory in
  `tests/conftest.py`, so `-m integration` / `-m e2e` cannot drop one.
- Real infra comes only from the `tests/infra.py` fixtures, registered for every test: `fresh_database` (an empty
  database per test, dropped after), `database_dsn` (one per module), `redis_url` (after a PING). Unset env skips;
  set env with an unreachable server calls `pytest.fail`. Each DB test gets its own database from these fixtures.
- `HYDRA_REQUIRE_INFRA=1` (CI) turns a skipped integration or e2e test, or a module skipped at collection, into a
  failure (`tests/conftest.py`; pinned by `tests/unit/test_repo/test_ci_gating.py`).
- Autouse fixtures in `tests/conftest.py`: `strategies_dir` redirects every writable config dir (strategies, ML
  models) into `tmp_path`, so no test writes the repo's `config/`
  (`tests/unit/test_dashboard/test_config_isolation.py::test_no_committed_test_strategy_yaml`);
  `offline_market_rules` gives backtests the fixture's Binance spot markets, never the public API.
- No test calls an exchange. The paper smoke (`tests/e2e/test_paper_smoke.py`) runs the real lifespan on TimescaleDB
  with the env romulus sets and replaces only the exchange: `tests/e2e/replay_exchange.py` replays the committed
  Binance bars in `tests/e2e/data/binance_btcusdt_1m.json` (provenance in its header) through ccxt-shaped fakes.
  `tests/e2e/test_split_smoke.py` drives the two-process layout (real trader and API over HTTP,
  `HYDRA_TRADER__URL`) on the same replay.
- Assert real numbers at the seam you changed: expected fills, prices, fees and balances are computed in the test
  from the fixture and the fill model, not mocked.
- `tests/unit/test_repo/` holds repo contracts that fail on doc or config drift: `test_readme_status.py` (every
  Working row in the README Status table names a proof that exists), `test_toolchain_pins.py` (Python, uv and image
  pins agree), `test_ci_gating.py` (publish needs every job; infra tiers cannot pass by skipping),
  `test_build_references.py` (every referenced Dockerfile exists), `test_version_single_source.py` (one version),
  `test_p5c_seam_ownership.py` (only the veto seam touches `hydra.ml`; the strategy package never imports it).
  Update them deliberately, never to get green.

## Where things are
- `tests/unit/test_<package>/` mirrors `src/hydra/<package>/`; `tests/unit/test_deploy/` pins the Prometheus rules
  and Grafana dashboards to `hydra.core.metrics`.
- `tests/fixtures/` exchange payloads (Binance spot, futures, klines), ONNX models, universe and sweeper data;
  `tests/integration/fixtures/` the 2.1.10 schema dump the migration tests upgrade.
