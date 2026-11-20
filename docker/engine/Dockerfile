FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928 AS builder
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --extra ml-training --no-install-project
COPY src/ src/
COPY config/ config/
COPY scripts/ scripts/
RUN uv sync --locked --no-dev --extra ml-training

FROM python:3.15-slim@sha256:65a65fc7b30b06cb6df4930fd4650697cb23f4c2698ab81fc87e4b031bc36928
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/config /app/config
COPY --from=builder /app/scripts /app/scripts
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://localhost:8000/livez')"]
CMD ["python", "-m", "hydra.dashboard.api"]
