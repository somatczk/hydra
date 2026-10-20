FROM nvidia/cuda:13.4.2-runtime-ubuntu22.04
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev python3-pip \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.13.3 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --extra ml-training --no-install-project
COPY src/ src/
COPY config/ config/
RUN uv sync --locked --extra ml-training
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python3.12", "-m", "hydra.ml.training"]
