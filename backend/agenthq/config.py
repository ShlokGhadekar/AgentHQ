"""Central configuration, read from environment variables (and a .env file for local runs).

Every tunable value lives here so it is easy to find and explain. Defaults match docker compose
with the ports published to localhost, so the backend also runs outside Docker.
"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed settings; field names map to upper-case env vars (llm_provider -> LLM_PROVIDER)."""

    # The .env file sits in the project root, one level above backend/.
    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    # --- Infrastructure ---
    database_url: str = "postgresql+asyncpg://agenthq:agenthq@localhost:5432/agenthq"
    redis_url: str = "redis://localhost:6379/0"
    repolens_url: str = "http://localhost:8000"

    # --- LLM ---
    llm_provider: Literal["groq", "anthropic"] = "groq"
    # Used by agents that don't set a model in agents.yaml.
    llm_model: str | None = None
    llm_timeout_s: float = 60.0
    llm_max_tokens: int = 4096
    # Retries for temporary LLM failures (rate limits, 5xx, timeouts). Free tiers rate-limit often,
    # so waits follow the provider's Retry-After when it sends one.
    llm_max_retries: int = 6
    llm_retry_base_s: float = 2.0
    llm_retry_max_wait_s: float = 60.0
    groq_api_key: str | None = None
    anthropic_api_key: str | None = None

    # --- Agent runtime ---
    # How long an agent's inbox read blocks before re-checking whether the run has ended.
    inbox_block_ms: int = 500
    # Rough cap on the tokens sent per LLM call. Groq's free tier rejects any request larger than
    # its 8,000 tokens-per-minute budget (HTTP 413), so the default leaves room for the reply.
    llm_context_budget_tokens: int = 4500

    # --- Guardrails (see guards.py). A "step" is one LLM call. ---
    max_steps_per_task: int = 30  # per agent per task attempt; crossing it fails the task
    max_steps_per_run: int = 200  # crossing it stops the run
    run_token_cap: int = 400_000  # tokens in + out over the whole run; crossing it stops the run
    max_task_attempts: int = 3  # the Manager can't retry a task more often than this
    max_messages_without_progress: int = 8  # between two agents about one task -> loop
    max_review_rounds: int = 3  # times the reviewer may send one task back
    max_failed_tool_calls_in_a_row: int = 6

    # Seconds per scripted LLM call in demo runs (FakeLLMClient), so a demo is watchable.
    demo_llm_delay_s: float = 0.6

    # --- GitHub (pull requests open only after a human approves) ---
    github_token: str | None = None

    # --- Workspace and sandbox ---
    # Each run clones the target repo into <workspaces_dir>/<run_id>.
    workspaces_dir: Path = Path(".workspaces")
    # Set when the backend runs in Docker: the named volume mounted at workspaces_dir, which the
    # sandbox containers mount too. Unset on the host, where the folder is bind-mounted instead.
    workspaces_volume: str | None = None
    sandbox_image: str = "agenthq-sandbox:latest"
    test_command: str = "pytest -q"
    lint_command: str = "ruff check ."
    command_timeout_s: int = 180

    # --- RepoLens (generous timeouts: indexing a repo can take minutes on a slow machine) ---
    repolens_index_timeout_s: float = 300
    repolens_ask_timeout_s: float = 60


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()
