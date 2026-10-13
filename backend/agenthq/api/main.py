"""AgentHQ HTTP API: start runs, watch their state, and control them (pause, resume, stop, approve)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agenthq.config import get_settings
from agenthq.registry import RunRegistry
from agenthq.runtime import Run
from agenthq.sandbox import remove_stale_sandboxes
from agenthq.tools import ApprovalError


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    await asyncio.to_thread(remove_stale_sandboxes)
    app.state.registry = RunRegistry(settings, client)
    yield
    await app.state.registry.shutdown()
    await client.aclose()


app = FastAPI(title="AgentHQ", lifespan=lifespan)


# --- Runs -----------------------------------------------------------------------

class StartRun(BaseModel):
    goal: str
    repo: str | None = None  # GitHub URL of the target repository
    demo: bool = False  # replay the scripted team: offline, no repo, no API key


class Decision(BaseModel):
    approved: bool
    comment: str = ""


@app.post("/runs", status_code=201)
async def start_run(body: StartRun, request: Request) -> dict[str, str]:
    registry: RunRegistry = request.app.state.registry
    try:
        run = registry.create(body.goal, body.repo, demo=body.demo)
    except ValueError as error:  # e.g. the LLM provider's API key is missing
        raise HTTPException(400, str(error)) from error
    # Setup (clone, sandbox, indexing) can take minutes, so the request returns at once and the
    # client follows progress through the event stream.
    registry.start_in_background(run)
    return {"run_id": run.run_id}


@app.get("/runs")
async def list_runs(request: Request) -> list[dict[str, Any]]:
    return [_summary(run) for run in request.app.state.registry.runs.values()]


@app.get("/runs/{run_id}")
async def get_run(run_id: str, request: Request) -> dict[str, Any]:
    run = _find(request, run_id)
    return {
        **_summary(run),
        "tasks": [{"id": t.id, "title": t.title, "assignee": t.assignee, "status": t.status,
                   "depends_on": t.depends_on, "attempts": t.attempts, "result": t.result}
                  for t in run.board.tasks.values()],
        "usage": {agent: {"tokens_in": u[0], "tokens_out": u[1]} for agent, u in run.usage.items()},
    }


@app.post("/runs/{run_id}/pause")
async def pause_run(run_id: str, request: Request) -> dict[str, str]:
    run = _find(request, run_id)
    await run.pause()
    return {"state": _state(run)}


@app.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, request: Request) -> dict[str, str]:
    run = _find(request, run_id)
    await run.resume()
    return {"state": _state(run)}


@app.post("/runs/{run_id}/stop")
async def stop_run(run_id: str, request: Request) -> dict[str, str]:
    run = _find(request, run_id)
    await run.stop("Stopped by the user")
    return {"state": _state(run)}


@app.post("/runs/{run_id}/approval")
async def decide(run_id: str, body: Decision, request: Request) -> dict[str, str | None]:
    """The human's decision on the changes. Approving is what opens the pull request."""
    run = _find(request, run_id)
    try:
        pr_url = await run.decide_approval(body.approved, body.comment)
    except ApprovalError as error:
        raise HTTPException(409, str(error)) from error
    return {"pr_url": pr_url}


def _find(request: Request, run_id: str) -> Run:
    try:
        return request.app.state.registry.get(run_id)
    except KeyError:
        raise HTTPException(404, f"No run {run_id}") from None


def _state(run: Run) -> str:
    if run.finished.is_set():
        return "complete"
    if run.approval_pending:
        return "awaiting_approval"
    return "running" if run.running.is_set() else "paused"


def _summary(run: Run) -> dict[str, Any]:
    return {"run_id": run.run_id, "goal": run.goal, "repo": run.repo, "state": _state(run)}


# --- Health ---------------------------------------------------------------------

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
