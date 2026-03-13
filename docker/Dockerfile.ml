FROM nvidia/cuda:12.4.1-runtime-ubuntu22.04
WORKDIR /app
RUN apt-get update && apt-get install -y python3.12 python3.12-venv python3-pip && rm -rf /var/lib/apt/lists/*
RUN pip install uv
COPY pyproject.toml .
RUN uv sync --extra ml-training --no-install-project
COPY src/ src/
COPY config/ config/
RUN uv sync --extra ml-training
ENV PATH="/app/.venv/bin:$PATH"
CMD ["python", "-m", "hydra.ml.training"]
