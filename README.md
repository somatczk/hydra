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
# Install dependencies
make dev

# Start local infrastructure
make dev-up

# Run tests
make test

# Run all checks
make check
```

## Architecture

See [CLAUDE.md](CLAUDE.md) for detailed architecture and conventions.
