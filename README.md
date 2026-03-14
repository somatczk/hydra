# Hydra

Bitcoin auto-trading platform with ML-assisted signals, multi-exchange support, backtesting with walk-forward/CPCV, and production monitoring.

## Features

- **Multi-exchange**: Binance, Bybit, Kraken, OKX (Spot + Futures) via CCXT
- **Strategy framework**: Code or no-code strategy creation, hot-reload, 7 built-in strategies
- **Backtesting**: Walk-forward analysis, CPCV, VectorBT parameter sweeps, realistic fill simulation
- **ML pipeline**: XGBoost/LSTM training, ONNX inference, drift detection, champion/challenger
- **Risk management**: 4-tier circuit breakers, pre-trade checks, exchange-side safety orders
- **Dashboard**: Next.js frontend with real-time WebSocket updates, TradingView charts
- **Monitoring**: Prometheus metrics, Grafana dashboards, Telegram alerts

## Quick Start

```bash
# Install dependencies (requires Python 3.12+ and uv)
make dev

# Start local TimescaleDB + Redis
make dev-up

# Run all checks (lint + type-check + security + tests)
make check
```

## Architecture

```
┌─────────────┐     ┌──────────────┐     ┌─────────────────┐
│  Dashboard   │────▶│  FastAPI API  │────▶│  TimescaleDB    │
│  (Next.js)   │◀────│  + WebSocket  │◀────│  (OHLCV + State)│
└─────────────┘     └──────┬───────┘     └─────────────────┘
                           │
                    ┌──────▼───────┐
                    │ Redis Streams │
                    │ (Event Bus)   │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐
        │ Strategy │ │   Risk   │ │ Backtest │
        │ Engine   │ │ Manager  │ │ Worker   │
        └────┬─────┘ └──────────┘ └──────────┘
             │
             ▼
        ┌──────────┐
        │   CCXT   │──▶ Binance / Bybit / Kraken / OKX
        └──────────┘
```

- **Event bus**: Redis Streams for async flows, direct function calls for sync hot paths
- **Database**: TimescaleDB — `ts` schema for hypertables (OHLCV), `public` for relational data
- **Safety**: Testnet + paper trading by default. Exchange-side stop-losses on every position.

## Project Structure

```
src/hydra/
├── core/          # Config, models, event bus, shared types
├── data/          # Market data ingestion (CCXT, WebSocket)
├── indicators/    # Technical indicators (TA-Lib + custom)
├── strategy/      # Strategy framework + built-in strategies
├── ml/            # ML pipeline (training, inference, drift)
├── backtest/      # Backtesting engine (walk-forward, CPCV)
├── execution/     # Order execution + exchange integration
├── risk/          # Risk management + circuit breakers
├── portfolio/     # Position tracking + PnL
├── dashboard/     # API routes + WebSocket handlers
└── ops/           # Health checks, metrics, alerts
dashboard/         # Next.js frontend
docker/            # Dockerfiles (engine, dashboard, backtest)
config/            # YAML config files
tests/             # Unit, integration, e2e, performance
```

## Development

```bash
make install       # Install dependencies
make dev           # Install with all extras (dev + ml-training)
make test          # Run unit tests with coverage
make lint          # Ruff check + format check
make type-check    # mypy strict mode
make security      # bandit security scan
make check         # All of the above
make dev-up        # Start local TimescaleDB + Redis
make dev-down      # Stop local infrastructure
make docker-build  # Build all Docker images
```

## Deployment

Docker images are published to GHCR on every merge to `main` with auto-incrementing semver tags. Deployed as a Docker Compose stack via [romulus](https://github.com/somatczk/romulus) (homelab infra).

## License

MIT
