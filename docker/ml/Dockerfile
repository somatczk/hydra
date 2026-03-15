FROM nvidia/cuda:13.1.1-runtime-ubuntu22.04
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        python3.12 python3.12-venv python3.12-dev python3-pip \
    && rm -rf /var/lib/apt/lists/*
RUN python3.12 -m pip install --no-cache-dir uv
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --extra ml-training --no-install-project
COPY src/ src/
COPY config/ config/
RUN uv sync --extra ml-training
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python3.12", "-m", "hydra.ml.training"]
