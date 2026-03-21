# Hydra

Bitcoin auto-trading platform with ML-assisted signals, multi-exchange support, backtesting with walk-forward/CPCV, and production monitoring.

## Features

- **Multi-exchange**: Binance, Bybit, Kraken, OKX (Spot + Futures) via CCXT
- **Strategy framework**: Visual no-code builder + Python strategies, hot-reload, 7 built-in strategies, 19 configs
- **Backtesting**: Walk-forward analysis, CPCV, VectorBT parameter sweeps, realistic fill simulation
- **ML pipeline**: XGBoost/LSTM training, ONNX inference with upload/promote/rollback, drift detection, ML overlay per strategy
- **Risk management**: 4-tier circuit breakers, pre-trade checks, per-strategy risk overrides, exchange-side safety orders, kill switch
- **Paper trading**: Per-strategy capital allocation, real PnL tracking (unrealized + realized), session detail with PnL %
- **Dashboard**: Next.js frontend with real-time WebSocket updates, TradingView charts, 10 pages
- **Monitoring**: Prometheus metrics, Grafana dashboards (Sharpe, drawdown, PnL), Telegram alerts

## Architecture

```mermaid
graph TB
    subgraph Frontend
        Dashboard[Next.js Dashboard<br/>10 pages]
    end

    subgraph Backend["FastAPI Backend"]
        API[REST API + WebSocket]
        Routes[Routes: strategies, models,<br/>trading, portfolio, risk, backtest]
    end

    subgraph Engine["Trading Engine"]
        Strategy[Strategy Engine<br/>7 built-in + rule-based builder]
        Risk[Risk Manager<br/>4-tier circuit breakers]
        Execution[Order Execution<br/>Paper + Live]
        ML[ML Pipeline<br/>ONNX inference + drift]
    end

    subgraph Data["Data Layer"]
        TimescaleDB[(TimescaleDB<br/>OHLCV + State)]
        Redis[(Redis Streams<br/>Event Bus + Cache)]
    end

    subgraph Exchanges
        CCXT[CCXT Pro] --> Binance & Bybit & Kraken & OKX
    end

    Dashboard <-->|HTTP + WS| API
    API --> Routes
    Routes --> Strategy & Risk & Execution & ML
    Strategy --> Redis
    Risk --> Redis
    Execution --> CCXT
    Strategy --> TimescaleDB
    Execution --> TimescaleDB
    ML -->|ONNX models| Strategy

    style Frontend fill:#1a1a2e,stroke:#8AB8FF,color:#fff
    style Backend fill:#1a1a2e,stroke:#22c55e,color:#fff
    style Engine fill:#1a1a2e,stroke:#f59e0b,color:#fff
    style Data fill:#1a1a2e,stroke:#a78bfa,color:#fff
    style Exchanges fill:#1a1a2e,stroke:#ef4444,color:#fff
```

### Data Flow

```mermaid
sequenceDiagram
    participant D as Dashboard
    participant A as FastAPI
    participant S as Strategy Engine
    participant R as Risk Manager
    participant E as Executor
    participant X as Exchange (CCXT)
    participant DB as TimescaleDB
    participant RD as Redis

    D->>A: Start paper/live session
    A->>S: Initialize strategy
    S->>RD: Subscribe to market data
    RD-->>S: OHLCV bar event
    S->>S: Evaluate conditions
    S->>R: Pre-trade risk check
    R-->>S: Approved / Rejected
    S->>E: Create order
    E->>X: Submit to exchange
    X-->>E: Fill confirmation
    E->>DB: Persist trade + balance snapshot
    E->>RD: Publish trade event
    RD-->>A: Trade notification
    A-->>D: WebSocket push
```

## Dashboard Pages

```mermaid
graph LR
    subgraph Pages
        Home["/  Dashboard"]
        Strategies["/strategies"]
        Builder["/builder"]
        Trading["/trading"]
        Session["/trading/[id]"]
        Backtest["/backtest"]
        Models["/models"]
        Portfolio["/portfolio"]
        Risk["/risk"]
        Settings["/settings"]
    end

    Strategies -->|"New Strategy"| Builder
    Strategies -->|"Start Session"| Trading
    Trading -->|"View Session"| Session
    Strategies -->|"Backtest"| Backtest
    Builder -->|"Attach Model"| Models

    style Pages fill:#1a1a2e,stroke:#8AB8FF,color:#fff
```

| Page | Description |
|------|-------------|
| `/` | Portfolio value, daily PnL chart, balance history, active positions |
| `/strategies` | Strategy cards with performance, per-strategy capital, start/stop sessions, risk overrides |
| `/builder` | Visual rule builder with indicators, conditions, timeframes, risk config, ML overlay |
| `/trading` | Live TradingView chart, open positions, recent trades, risk controls, kill switch |
| `/trading/[id]` | Session detail with starting capital, PnL %, positions, trade history, WebSocket live updates |
| `/backtest` | Run backtests with walk-forward/CPCV, equity curves, trade analysis |
| `/models` | ML model registry with ONNX upload, promote/rollback, accuracy chart, drift monitoring |
| `/portfolio` | Detailed position breakdown, allocation chart, balance snapshots |
| `/risk` | Circuit breaker status, risk limits, daily PnL vs limits |
| `/settings` | System config, exchange credentials, trading mode |

## Project Structure

```
src/hydra/
├── core/           # Config, models, event bus, shared types
├── data/           # Market data ingestion (CCXT, WebSocket)
├── indicators/     # Technical indicators (TA-Lib + custom)
├── strategy/       # Strategy framework, rule-based builder, 7 built-in strategies
│   └── builtin/    # breakout, composite, mean_reversion, ml_ensemble,
│                   # momentum, rule_based, trend_following
├── ml/             # Training, ONNX serving, drift detection, feature engineering
├── backtest/       # Backtesting engine (walk-forward, CPCV)
├── execution/      # Paper trading, live execution, session manager
├── risk/           # Circuit breakers, pre-trade checks, position sizing, exchange safety
├── portfolio/      # Position tracking + PnL calculation
├── dashboard/      # FastAPI routes + WebSocket handlers
│   └── routes/     # strategies, models, trading, portfolio, risk, backtest, system
└── ops/            # Health checks, Prometheus metrics, Telegram alerts
dashboard/          # Next.js 15 frontend (React 19, Tailwind CSS 4, Recharts)
docker/             # Dockerfiles (engine, dashboard, backtest, ml)
config/             # YAML configs (base, live, backtest, 19 strategy configs)
tests/              # Unit (849+), integration, e2e, performance
```

## Tech Stack

```mermaid
graph LR
    subgraph Backend
        Python[Python 3.12+]
        FastAPI[FastAPI]
        SQLAlchemy[SQLAlchemy 2.0]
        CCXT[CCXT Pro]
    end

    subgraph Frontend
        Next[Next.js 15]
        React[React 19]
        Tailwind[Tailwind CSS 4]
        TV[TradingView Charts]
    end

    subgraph ML
        XGBoost
        PyTorch[PyTorch LSTM]
        ONNX[ONNX Runtime]
    end

    subgraph Infra
        TimescaleDB
        Redis
        Docker
        Prometheus
        Grafana
    end

    style Backend fill:#1a1a2e,stroke:#22c55e,color:#fff
    style Frontend fill:#1a1a2e,stroke:#8AB8FF,color:#fff
    style ML fill:#1a1a2e,stroke:#f59e0b,color:#fff
    style Infra fill:#1a1a2e,stroke:#a78bfa,color:#fff
```

## Quick Start

```bash
# Install dependencies (requires Python 3.12+ and uv)
make dev

# Start local TimescaleDB + Redis
make dev-up

# Run all checks (lint + type-check + security + tests)
make check
```

## Development

```bash
make install       # Install dependencies
make dev           # Install with all extras (dev + ml-training)
make test          # Run unit tests with coverage
make unit          # Unit tests only
make integration   # Integration tests only
make e2e           # End-to-end tests
make perf          # Performance benchmarks
make lint          # Ruff check + format check
make format        # Auto-format code
make type-check    # mypy strict mode
make security      # bandit security scan
make imports       # Import boundary checks
make check         # All: lint + type-check + security + test
make dev-up        # Start local TimescaleDB + Redis
make dev-down      # Stop local infrastructure
make docker-build  # Build all Docker images
make clean         # Remove build artifacts
```

## CI/CD

```mermaid
graph LR
    PR[Pull Request] --> CI[CI Pipeline]
    CI --> Lint[Ruff lint + format]
    CI --> Types[mypy strict]
    CI --> Security[bandit scan]
    CI --> Tests[Unit + Integration + E2E]

    Merge[Merge to main] --> Build[Build Docker Images]
    Build --> GHCR[Push to GHCR<br/>hydra-engine, hydra-dashboard,<br/>hydra-backtest]
    GHCR --> Dependabot[Dependabot PR<br/>in romulus]
    Dependabot --> Deploy[Docker Compose<br/>on ZimaOS]

    style CI fill:#1a1a2e,stroke:#22c55e,color:#fff
    style Build fill:#1a1a2e,stroke:#8AB8FF,color:#fff
```

- PRs trigger lint, type-check, security scan, unit/integration/e2e tests
- Merge to `main` builds Docker images, pushes to GHCR with auto-incrementing semver tags
- Deployed via [romulus](https://github.com/somatczk/romulus) as a Docker Compose stack on ZimaOS

## License

MIT
