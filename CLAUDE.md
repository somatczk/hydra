# Hydra Trading Platform

## Overview
Bitcoin trading platform under active rebuild (phases P0 to P5, plus MCP after P2). Today it runs paper-trading
sessions with rule-based strategies on Binance market data, and backtests on bars stored in TimescaleDB. Live trading
is not supported until P3. What works and what does not is kept in one place, the **Status** table in `README.md`. Do
not duplicate it here; update that table in the PR that changes a row.

## Working rules (every agent session in this repo)
- **Commits**: Conventional Commits. No `Co-Authored-By` trailer, no Claude or session lines.
- **PRs**: no "Generated with Claude Code" footer and no session links. The body says what and why, the audit IDs
  closed, the test evidence (commands and results) and any deviations from the plan. One PR per workstream.
- **Gates before every commit**, all green:
  - Python: `uv sync --locked --extra dev`, then `uv run ruff check .`, `uv run ruff format --check .`,
    `uv run mypy src/`, `uv run pytest tests/unit -q -m "not slow"`, plus any integration or e2e tests you touched.
  - Dashboard, when anything under `dashboard/` changed:
    `cd dashboard && npm ci && npm run lint && npx tsc --noEmit && npm test && npm run build`.
  - `git status --porcelain --untracked-files=all` must be clean after the tests (CI enforces this).
- **Never silence a check**: no `noqa`, `type: ignore`, `eslint-disable`, `pytest.skip`/`xfail` or `.skip` to get
  green. Fix the cause.
- **Comments**: at most one short comment line where it is truly needed. Reasoning goes in the PR body.
- **Tests** assert real behaviour and real numbers at the seam being fixed; do not mock the seam away.
- **Integration tests** must `pytest.fail`, not skip, when their DB env (`HYDRA_DB_*`) is set but the server is
  unreachable, and they build their own DSN. `tests/integration/test_event_bus_redis.py` predates this rule and
  still skips.
- **Releases** only at the end of a phase: the `pyproject.toml` version, `uv lock` and the romulus image tags move
  together. Never bump the version inside a feature PR.
- **Worktrees**: work in `~/Documents/GitHub/.wt/<repo>-<ws-id>` on branch `ws/<ws-id>`, never in the main checkout.
  Do not run `tests/unit/test_dashboard` from a non-worktree checkout.
- **Infra**: do not run `make dev-up` or other Docker commands unless the task explicitly owns infrastructure.
- **Secrets**: never print `.env` contents or keys. Never call exchange APIs with real keys.

## Tech stack
- **Python**: images run 3.14; `requires-python = ">=3.12"` and ruff/mypy target 3.12; the ML image uses 3.12. The
  pin is a P1 ops item.
- **Backend**: FastAPI + uvicorn, raw asyncpg with inline DDL in `src/hydra/dashboard/api.py`, pydantic. SQLAlchemy
  and alembic are declared but not used at runtime.
- **Exchange**: ccxt async (REST) and ccxt.pro (session market-data feed).
- **Database**: TimescaleDB (PostgreSQL 16). `ts` schema for hypertables (`ts.ohlcv_1m`, `ts.funding_rates`),
  `public` for relational data.
- **Frontend**: Next.js 16.4, React 19, TypeScript 7 for `tsc --noEmit` (lint and `next build` use the TypeScript 6
  API package), Tailwind CSS 4, Recharts, vitest + jsdom. The TradingView chart on `/trading` is a third-party embed.
- **ML**: onnxruntime, plus xgboost/torch/mlflow in the `ml-training` extra. Not wired into trading (P5).

## Project structure
- `src/hydra/`: 11 modules.
  - `core`: config, domain types, events, event buses, WebSocket manager.
  - `data`: ingestion, bar storage, backfill, news and sentiment.
  - `indicators`: TA-Lib wrappers and custom indicators.
  - `strategy`: base class, engine, rule engine, YAML config, `builtin/`.
  - `backtest`: runner, bar loader, fills, metrics, hyperopt, walk-forward.
  - `execution`: SessionManager, OrderManager, paper executor, ExchangeClient.
  - `risk`: pre-trade checks, sizing, circuit breakers, exchange safety.
  - `portfolio`: P&L, positions, reconciliation.
  - `ml`: features, serving, export, registry, drift, training.
  - `dashboard`: the FastAPI app, `routes/`, Prometheus `metrics.py`.
  - `ops`: Discord/Slack webhook notifier.
- `dashboard/`: Next.js app (see `dashboard/README.md`).
- `config/`: base/live/backtest YAML, `strategies/`, `templates/`.
- `deploy/`: local compose, Prometheus rules and promtool tests, Grafana dashboard.
- `docker/<image>/Dockerfile`: engine, dashboard, backtest, ml.
- `tests/`: `unit/`, `integration/`, `e2e/`, `performance/`.

## Commands
```bash
uv sync --locked --extra dev   # what CI installs
make lint                      # ruff check + format check (src/ tests/)
make type-check                # mypy src/
make security                  # bandit
make test                      # unit tests, not slow, with coverage
make e2e                       # e2e tests, no infrastructure
make integration               # integration tests; DB tests need HYDRA_DB_*
make check                     # lint + type-check + security + test
make imports                   # import-linter; fails until P1
make docker-build              # needs Docker
```
CI also runs `uv run mypy src/hydra/core/ src/hydra/data/ src/hydra/indicators/ src/hydra/strategy/
src/hydra/backtest/ src/hydra/execution/ src/hydra/risk/ src/hydra/portfolio/ --strict`. Run it when you touch
those packages, because `mypy src/` alone does not catch everything it catches.

## Architecture (as is)
- One engine process holds FastAPI, the SessionManager and an `InMemoryEventBus` per session. Redis is only
  health-pinged; it is not on the trading path.
- Signal path: ccxt.pro bar → StrategyEngine → OrderManager (pre-trade check by PreTradeRiskManager) →
  PaperTradingExecutor or ExchangeClient. ExchangeClient defaults to `testnet=True`.
- Persisted: risk config and kill-switch flag, trading sessions, paper trades, positions and balance snapshots,
  backtest results and trades with data provenance. In memory only: system config, background task state
  (backtest, hyperopt, walk-forward). Circuit breakers are not instantiated. Paper state is not reloaded on restart.
- No exchange-side stops yet (P3). Only Binance is supported in practice; the explicit gate is P1.
- Errors from dashboard routes use one envelope, `{"detail": {"code", "message", "state", ...}}`, built by
  `src/hydra/dashboard/routes/_db.py`; the frontend reads it with `dashboard/src/lib/apiState.ts`. Reuse both.
- Backtests run only on stored bars (`hydra.backtest.data`); no data means 422, never a synthetic fallback.
- The app version comes from `importlib.metadata.version("hydra")`, so `pyproject.toml` is the single source.

## Code conventions
- Frozen dataclasses for value objects; Decimal in domain types and fills (floats remain at some boundaries).
- Pydantic for config and request validation. asyncio throughout.
- Type hints on new code. mypy: `pyproject.toml` sets strict with relaxed overrides for most packages; CI runs the
  strict command above.
- import-linter (`[tool.importlinter]`) describes the intended layers, but the contract is broken today and it is not
  in CI (P1 ops). Do not add new cross-layer imports.
- New code comes with tests. Not every existing module has them.

## Versioning and release
- The version lives only in `pyproject.toml` `[project] version`. After changing it, run `uv lock` so `uv.lock`
  matches; CI uses `uv sync --locked` and fails on drift.
- Bumps happen only at phase end. The same release moves the romulus image tags for engine, dashboard and
  backtest-worker (`romulus/stacks/hydra/*/Dockerfile`) in one romulus PR.
- `dashboard/package.json` has its own version, shown in the sidebar; it is not the release version.

## CI/CD
- `ci.yml` on PRs and pushes to `main`: ruff, strict mypy over eight packages, bandit, unit tests with a clean-tree
  check, dashboard (lint, typecheck, test, build), integration (TimescaleDB + Redis containers; the Redis tests skip
  because they probe `localhost:6379`), e2e (no infrastructure used).
- `rules.yml`: `promtool check rules` and `promtool test rules` for `deploy/prometheus-rules.yml`.
- `build.yml` on pushes to `main` touching `src/`, `docker/`, `dashboard/`, `pyproject.toml` or `uv.lock`: builds
  `hydra-engine`, `hydra-dashboard`, `hydra-backtest`, pushes `main` and `sha-<7>`, and pushes the version tag only if
  it does not exist yet. Version tags never move. The build is not gated on CI (P1 ops).
- `dependabot-auto-merge.yml`: Dependabot minor/patch PRs merge on green CI; majors, 0.x minors, pre-releases and base
  images wait for review.

## Deployment
romulus (homelab infra repo) runs the `hydra` stack on a ZimaCube with Ubuntu 26.04, applied by doco-cd. Images come
from `ghcr.io/somatczk/hydra-*`; romulus Dependabot opens the tag-bump PRs. Romulus P0-E2 (in progress) holds hydra
bumps back from auto-merge, drops the public `/health` + `/metrics` router and removes the Telegram env. The engine
reads `DATABASE_URL`, `REDIS_URL`, `LOG_LEVEL` and `LOG_FORMAT` (romulus sets the first two), and the host bind-mounts
its own `/app/config` over the image's.
