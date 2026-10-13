FROM python:3.12-slim

# git clones the target repo into each run's workspace.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependencies first so code edits don't invalidate this layer.
COPY pyproject.toml .
RUN mkdir agenthq && touch agenthq/__init__.py \
    && pip install --no-cache-dir . \
    && rm -rf agenthq

COPY agenthq ./agenthq
RUN pip install --no-cache-dir --no-deps .

EXPOSE 8080
CMD ["uvicorn", "agenthq.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
