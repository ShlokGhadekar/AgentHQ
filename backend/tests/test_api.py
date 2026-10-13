"""The run API, driving a scripted demo run end to end over HTTP."""

import asyncio
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agenthq.api.main import app
from agenthq.config import Settings
from agenthq.registry import RunRegistry


@pytest.fixture
async def client(redis: FakeRedis) -> AsyncIterator[httpx.AsyncClient]:
    # Set up the registry directly instead of through the lifespan, which needs real services.
    app.state.registry = RunRegistry(Settings(_env_file=None, inbox_block_ms=20, demo_llm_delay_s=0), redis)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    await app.state.registry.shutdown()


async def wait_for_state(client: httpx.AsyncClient, run_id: str, state: str) -> dict:
    async with asyncio.timeout(10):
        while True:
            run = (await client.get(f"/runs/{run_id}")).json()
            if run["state"] == state:
                return run
            await asyncio.sleep(0.02)


async def test_demo_run_through_the_api(client: httpx.AsyncClient) -> None:
    response = await client.post("/runs", json={"goal": "Add rate limiting to /login", "demo": True})
    assert response.status_code == 201
    run_id = response.json()["run_id"]

    run = await wait_for_state(client, run_id, "awaiting_approval")
    assert {t["status"] for t in run["tasks"]} == {"done"}

    decision = await client.post(f"/runs/{run_id}/approval", json={"approved": True})
    assert decision.json() == {"pr_url": None}  # demo runs have no repository to open a PR on
    run = await wait_for_state(client, run_id, "complete")
    assert set(run["usage"]) == {"manager", "researcher", "engineer", "reviewer"}
    assert [r["run_id"] for r in (await client.get("/runs")).json()] == [run_id]


async def test_pause_resume_and_stop(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/runs", json={"goal": "g", "demo": True})).json()["run_id"]
    assert (await client.post(f"/runs/{run_id}/pause")).json() == {"state": "paused"}
    assert (await client.post(f"/runs/{run_id}/resume")).json()["state"] in ("running", "awaiting_approval")
    assert (await client.post(f"/runs/{run_id}/stop")).json() == {"state": "complete"}


async def test_unknown_run_is_404_and_early_decision_is_409(client: httpx.AsyncClient) -> None:
    assert (await client.get("/runs/run-nope")).status_code == 404
    run_id = (await client.post("/runs", json={"goal": "g", "demo": True})).json()["run_id"]
    await client.post(f"/runs/{run_id}/pause")
    response = await client.post(f"/runs/{run_id}/approval", json={"approved": True})
    assert response.status_code == 409
    await client.post(f"/runs/{run_id}/stop")


async def test_team_cards(client: httpx.AsyncClient) -> None:
    agents = (await client.get("/agents")).json()
    assert [a["name"] for a in agents] == ["manager", "researcher", "engineer", "reviewer"]
    assert "request_approval" in agents[0]["tools"]


async def test_chat_reaches_the_agent(client: httpx.AsyncClient) -> None:
    run_id = (await client.post("/runs", json={"goal": "g", "demo": True})).json()["run_id"]
    await client.post(f"/runs/{run_id}/pause")
    response = await client.post(f"/runs/{run_id}/messages", json={"to": "researcher", "content": "Which file has login?"})
    assert response.status_code == 202
    run = app.state.registry.get(run_id)
    assert any(e.sender == "human" and e.recipient == "researcher" for e in run.history)
    assert (await client.post(f"/runs/{run_id}/messages", json={"to": "intern", "content": "hi"})).status_code == 400
    await client.post(f"/runs/{run_id}/stop")
    assert (await client.post(f"/runs/{run_id}/messages", json={"to": "researcher", "content": "hi"})).status_code == 409


@pytest.fixture
def ws_client() -> Iterator[TestClient]:
    """A synchronous client for WebSocket tests, with its own registry on fake Redis."""
    app.state.registry = RunRegistry(Settings(_env_file=None, inbox_block_ms=20, demo_llm_delay_s=0.01),
                                     FakeRedis(decode_responses=True))
    with TestClient(app) as http:
        yield http
    app.state.registry = None


def test_websocket_sends_history_then_live_events(ws_client: TestClient) -> None:
    run_id = ws_client.post("/runs", json={"goal": "g", "demo": True}).json()["run_id"]
    seen = []
    with ws_client.websocket_connect(f"/ws/runs/{run_id}") as ws:
        while True:
            event = ws.receive_json()
            seen.append(event)
            if event["type"] == "approval_requested":
                ws_client.post(f"/runs/{run_id}/approval", json={"approved": True})
            if event["type"] == "run_complete":
                break
    types = [e["type"] for e in seen]
    assert types[0] == "status_change" and types[-1] == "run_complete"
    assert len({e["id"] for e in seen}) == len(seen)  # nothing sent twice
    assert "approval_decision" in types


def test_websocket_rejects_unknown_runs(ws_client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), ws_client.websocket_connect("/ws/runs/run-nope") as ws:
        ws.receive_json()
