"""AgentHQ HTTP API: start runs, watch their state, and control them (pause, resume, stop, approve)."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import redis.asyncio as redis
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from agenthq.agent import load_agent_configs
from agenthq.bus import latest_event_id, read_events
from agenthq.config import get_settings
from agenthq.events import Event, EventType
from agenthq.registry import RunRegistry
from agenthq.runtime import Run
from agenthq.sandbox import remove_stale_sandboxes
from agenthq.storage import EventRecorder, EventStore
from agenthq.tools import ApprovalError

# Close codes for /ws/runs/{id}, so the UI can tell what happened. 4001: the run is finished or was
# interrupted, everything recorded has been sent, and nothing more will come. 4404: no such run.
ARCHIVED = 4001
UNKNOWN_RUN = 4404


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if getattr(app.state, "registry", None) is not None:  # tests install their own (fake Redis, no Docker)
        yield
        return
    settings = get_settings()
    client = redis.from_url(settings.redis_url, decode_responses=True)
    engine = create_async_engine(settings.database_url)
    app.state.store = EventStore(engine)
    await app.state.store.create_tables()
    # One recorder per backend process copies the event stream into Postgres (see storage.py).
    recorder = asyncio.create_task(EventRecorder(client, app.state.store).run_forever(), name="recorder")
    await asyncio.to_thread(remove_stale_sandboxes)
    app.state.registry = RunRegistry(settings, client)
    yield
    await app.state.registry.shutdown()
    recorder.cancel()
    await engine.dispose()
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


class ChatMessage(BaseModel):
    to: str
    content: str = Field(min_length=1)


@app.get("/agents")
async def list_agents() -> list[dict[str, Any]]:
    """The team's capability cards, so a UI can draw the team before any run starts."""
    return [{**config.card(), "model": config.model} for config in load_agent_configs()]


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
    """Recorded runs from Postgres plus the live ones in this process, oldest first."""
    runs: dict[str, dict[str, Any]] = {}
    store: EventStore | None = getattr(request.app.state, "store", None)
    if store:
        for record in await store.runs():
            runs[record.id] = {
                "run_id": record.id, "goal": record.goal, "repo": record.repo, "created_at": record.created_at.isoformat(),
                # Recorded but neither finished nor live: the server stopped while it was running.
                "state": "complete" if record.finished_at else "interrupted",
            }
    for run in request.app.state.registry.runs.values():
        runs[run.run_id] = _summary(run)  # a live run's own state is the most current
    return sorted(runs.values(), key=lambda r: r["created_at"])


@app.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request) -> list[dict[str, Any]]:
    """Every event of a run in order, for replay. Live runs come from memory, past ones from Postgres."""
    events = await _history(request.app, run_id)
    if events is None:
        raise HTTPException(404, f"No run {run_id}")
    return [event.model_dump(mode="json") for event in events]


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


@app.post("/runs/{run_id}/messages", status_code=202)
async def send_chat(run_id: str, body: ChatMessage, request: Request) -> dict[str, str]:
    """A message from the human to one agent, delivered to that agent's inbox."""
    run = _find(request, run_id)
    if body.to not in run.agents:
        raise HTTPException(400, f"Unknown agent {body.to!r}. Agents: {', '.join(run.agents)}")
    if run.finished.is_set():
        raise HTTPException(409, "The run has finished")
    await run.publish(Event(run_id=run.run_id, type=EventType.MESSAGE, sender="human", recipient=body.to,
                            payload={"content": body.content}))
    return {"status": "delivered"}


@app.websocket("/ws/runs/{run_id}")
async def stream_run(websocket: WebSocket, run_id: str) -> None:
    """Send a run's events as JSON: everything so far, then each new event as it is published."""
    registry: RunRegistry = websocket.app.state.registry
    # Accept before closing: a connection refused during the handshake reaches the browser as a
    # generic failure (code 1006), and the UI couldn't tell "no such run" from "server down".
    await websocket.accept()
    if run_id not in registry.runs:
        # Not running here: send what was recorded, if anything, and say that's all there is.
        recorded = await _history(websocket.app, run_id)
        for event in recorded or []:
            await websocket.send_text(event.model_dump_json())
        await websocket.close(code=ARCHIVED if recorded else UNKNOWN_RUN, reason="Archived run" if recorded else f"No run {run_id}")
        return
    run = registry.get(run_id)
    # Note where the global stream ends *before* copying the history, so an event published in
    # between can't be missed; one that shows up in both is skipped by its id.
    cursor = await latest_event_id(registry.redis)
    sent: set[str] = set()
    for event in list(run.history):
        await websocket.send_text(event.model_dump_json())
        sent.add(event.id)

    async def forward_new_events() -> None:
        nonlocal cursor
        while True:
            for entry_id, event in await read_events(registry.redis, cursor, block_ms=1000):
                cursor = entry_id
                if event.run_id == run_id and event.id not in sent:
                    await websocket.send_text(event.model_dump_json())

    async def wait_for_disconnect() -> None:
        # Without this, a closed tab is only noticed on the next send, which may never come.
        while True:
            await websocket.receive_text()

    tasks = [asyncio.create_task(forward_new_events()), asyncio.create_task(wait_for_disconnect())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            task.cancel()


async def _history(app: FastAPI, run_id: str) -> list[Event] | None:
    """A run's events: from memory if it is live here, else from Postgres. None if it is unknown."""
    if run_id in app.state.registry.runs:
        return list(app.state.registry.get(run_id).history)
    store: EventStore | None = getattr(app.state, "store", None)
    events = await store.events(run_id) if store else []
    return events or None


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
    return {"run_id": run.run_id, "goal": run.goal, "repo": run.repo, "state": _state(run),
            "created_at": run.created_at.isoformat()}


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
