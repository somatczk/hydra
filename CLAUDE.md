# Hydra Trading Platform

## Overview
Bitcoin auto-trading platform with ML-assisted signals, multi-exchange support (Binance/Bybit/Kraken/OKX), backtesting, and production monitoring.

## Tech Stack
- **Backend**: Python 3.12+, FastAPI, asyncpg, SQLAlchemy 2.0 (async), Redis Streams
- **Frontend**: Next.js 15, React 19, Tailwind CSS 4, Recharts, TradingView Lightweight Charts
- **Database**: TimescaleDB (PostgreSQL 16 + hypertables)
- **ML**: XGBoost, PyTorch (LSTM), ONNX Runtime, MLflow
- **Exchange**: CCXT Pro (unified multi-exchange API)
- **Infra**: Docker, Prometheus, Grafana

## Project Structure
- `src/hydra/` — Python package (11 modules: core, data, indicators, strategy, ml, backtest, execution, risk, portfolio, dashboard, ops)
- `dashboard/` — Next.js frontend app
- `config/` — YAML configuration files
- `tests/` — pytest tests (unit, integration, e2e, performance)
- `docker/` — Dockerfiles for each service
- `scripts/` — Utility scripts

## Commands
```bash
make install          # Install dependencies
make dev              # Install with all extras (dev + ml-training)
make test             # Run unit tests with coverage
make lint             # Ruff check + format check
make type-check       # mypy strict mode
make security         # bandit security scan
make check            # All: lint + type-check + security + test
make dev-up           # Start local TimescaleDB + Redis
make dev-down         # Stop local infrastructure
make docker-build     # Build all Docker images
```

## Architecture
- **Event bus**: Redis Streams for async flows, direct function calls for sync hot paths (risk checks)
- **Database**: Single TimescaleDB instance — `ts` schema for hypertables (OHLCV), `public` for relational data
- **PostgreSQL is source of truth** for all financial state. Redis is event bus + read cache.
- **CCXT** abstracts exchange differences. Strategies are exchange-agnostic.
- **Safety**: Testnet + paper trading by default. Exchange-side stop-losses on every position.

## Code Conventions
- Frozen dataclasses for value objects, Decimal for financial precision
- structlog for logging (JSON in prod, colored in dev)
- Pydantic for all config/validation
- asyncio throughout — async/await by default
- Type hints on everything, mypy strict mode
- Tests alongside code — every module has tests

## Module Boundaries
Each module only imports from `hydra.core` + its declared dependencies. Enforced by import-linter.

## Versioning
- Version is defined in `pyproject.toml` under `[project] version`
- When bumping the version, **both `pyproject.toml` AND `uv.lock` must be updated**. Run `uv lock` after changing the version in `pyproject.toml` to sync the lockfile.
- After pushing to hydra, bump the image tags in `romulus/stacks/hydra/*/Dockerfile` (engine, dashboard, backtest-worker) to match.

## CI/CD
- PRs trigger lint, type-check, security scan, unit/integration/e2e tests
- Merge to `main` builds Docker images, pushes to GHCR with auto-incrementing semver tags
- Dependabot in the romulus repo picks up new image versions weekly and opens PRs

## Deployment
Deployed via romulus (homelab infra repo) as a Docker Compose stack on ZimaOS. Images are pulled from `ghcr.io/somatczk/hydra-*`.
