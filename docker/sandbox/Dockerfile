# Image for the per-run sandbox containers that run the target repo's tests and linter.
# Test tools are baked in, so a run only installs the repo's own dependencies.
FROM python:3.12-slim
# Pinned so lint and test results only change when we decide to upgrade.
RUN pip install --no-cache-dir pytest==9.1.1 ruff==0.17.0
WORKDIR /workspace
CMD ["sleep", "infinity"]
