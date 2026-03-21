FROM python:3.13-slim AS builder
WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --no-dev --extra ml-training --no-install-project
COPY src/ src/
COPY config/ config/
RUN uv sync --no-dev --extra ml-training

FROM python:3.13-slim
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/config /app/config
ENV PATH="/app/.venv/bin:$PATH"
HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
    CMD ["python", "-c", "print('ok')"]
CMD ["python", "-m", "hydra.backtest.runner"]
