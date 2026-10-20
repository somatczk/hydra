FROM python:3.14-slim AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra ml-training --no-install-project
COPY src/ src/
COPY config/ config/
RUN uv sync --locked --no-dev --extra ml-training

FROM python:3.14-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/config /app/config
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "hydra.backtest.runner"]
