"""App settings, read from environment variables."""

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    token_ttl_minutes: int = int(os.getenv("TOKEN_TTL_MINUTES", "60"))
    max_title_length: int = int(os.getenv("MAX_TITLE_LENGTH", "200"))
    max_body_length: int = int(os.getenv("MAX_BODY_LENGTH", "5000"))


settings = Settings()
