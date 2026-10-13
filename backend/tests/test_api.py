"""The run API, driving a scripted demo run end to end over HTTP."""

import asyncio
from collections.abc import AsyncIterator

import httpx
import pytest
from fakeredis.aioredis import FakeRedis

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
