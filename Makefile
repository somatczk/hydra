.PHONY: install dev test lint type-check security check unit integration e2e smoke perf \
	dev-up dev-down format clean docker-build imports

# Development setup
install:
	uv sync --locked

dev:
	uv sync --locked --all-extras

# Testing
test:
	uv run pytest tests/unit/ -m "not slow" --cov

unit:
	uv run pytest tests/unit/ -v

integration:
	uv run pytest tests/integration/ -v -m integration

e2e:
	uv run pytest tests/e2e/ -v -m e2e

# Paper smoke: real lifespan on TimescaleDB (HYDRA_DB_*, HYDRA_REDIS_URL), replayed Binance bars
smoke:
	uv run pytest tests/e2e/test_paper_smoke.py -v -m e2e

perf:
	uv run pytest tests/performance/ -v -m performance --benchmark-json=benchmark.json

# Code quality
lint:
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format:
	uv run ruff check --fix src/ tests/
	uv run ruff format src/ tests/

type-check:
	uv run mypy src/

security:
	uv run bandit -r src/ -c pyproject.toml

imports:
	uv run lint-imports

# All checks
check: lint type-check security imports test

# Infrastructure
dev-up:
	docker compose -f docker-compose.dev.yml up -d

dev-down:
	docker compose -f docker-compose.dev.yml down

# Docker
docker-build:
	docker build -f docker/engine/Dockerfile -t hydra-engine .
	docker build -f docker/dashboard/Dockerfile -t hydra-dashboard .
	docker build -f docker/backtest/Dockerfile -t hydra-backtest .

# Cleanup
clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf .coverage htmlcov/ dist/ build/
