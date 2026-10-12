"""AgentHQ HTTP API. Phase 1 only has a health check that proves every service is reachable."""

from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agenthq.config import get_settings

app = FastAPI(title="AgentHQ")


@app.get("/health")
async def health() -> dict[str, Any]:
    """Check Postgres, Redis and RepoLens, and report each one separately.

    Always returns 200 with per-service results, so one broken dependency doesn't hide the others.
    """
    settings = get_settings()
    return {
        "postgres": await _check_postgres(settings.database_url),
        "redis": await _check_redis(settings.redis_url),
        "repolens": await _check_repolens(settings.repolens_url),
    }


async def _check_postgres(url: str) -> dict[str, Any]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as error:  # any failure means "not reachable"; the message says why
        return {"ok": False, "error": str(error)}
    finally:
        await engine.dispose()


async def _check_redis(url: str) -> dict[str, Any]:
    client = redis.from_url(url)
    try:
        await client.ping()
        return {"ok": True}
    except Exception as error:
        return {"ok": False, "error": str(error)}
    finally:
        await client.aclose()


async def _check_repolens(url: str) -> dict[str, Any]:
    """GET /status also tells us which repo RepoLens currently has indexed."""
    try:
        async with httpx.AsyncClient(base_url=url, timeout=5) as client:
            response = await client.get("/status")
            response.raise_for_status()
        return {"ok": True, "status": response.json()}
    except Exception as error:
        return {"ok": False, "error": str(error)}
