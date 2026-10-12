"""Central configuration, read from environment variables (and a .env file for local runs).

Every tunable value lives here so it is easy to find and explain. Defaults match docker compose
with the ports published to localhost, so the backend also runs outside Docker.
"""

from functools import lru_cache
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
    groq_api_key: str | None = None
    anthropic_api_key: str | None = None


@lru_cache
def get_settings() -> Settings:
    """Load settings once per process."""
    return Settings()
