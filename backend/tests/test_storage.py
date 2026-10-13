"""Run history in a database: recording the stream, the runs table, resuming, replay order.

Runs on in-memory SQLite; production uses Postgres with the same code.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import create_async_engine

from agenthq.bus import RedisBus, read_events
from agenthq.llm.fake import load_scripts
from agenthq.registry import DEMO_SCRIPT
from agenthq.storage import EventRecorder, EventStore
from tests.conftest import RunFactory, run_to_end


@pytest.fixture
async def store() -> AsyncIterator[EventStore]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    store = EventStore(engine)
    await store.create_tables()
    yield store
    await engine.dispose()


async def record_everything(redis: FakeRedis, store: EventStore) -> None:
    await store.record(await read_events(redis, await store.cursor(), block_ms=10))


async def test_a_recorded_run_replays_exactly(make_run: RunFactory, redis: FakeRedis, store: EventStore) -> None:
    run = make_run(load_scripts(DEMO_SCRIPT))
    live = await run_to_end(run, redis)
    await record_everything(redis, store)

    stored = await store.events("run-test")
    assert [e.id for e in stored] == [e.id for e in live]  # same events, same order
    assert stored == live  # and nothing lost in the round trip (payloads, tokens, timestamps)


async def test_runs_table_is_built_from_the_events(make_run: RunFactory, redis: FakeRedis, store: EventStore) -> None:
    await run_to_end(make_run(load_scripts(DEMO_SCRIPT)), redis)
    await record_everything(redis, store)
    [run] = await store.runs()
    assert run.id == "run-test"
    assert run.goal == "Add rate limiting to /login"  # carried by the run's first event
    assert run.finished_at is not None and not run.failed
    assert "rate limited" in (run.report or "")


async def test_recording_the_same_batch_twice_is_harmless(redis: FakeRedis, store: EventStore, make_run: RunFactory) -> None:
    await run_to_end(make_run(load_scripts(DEMO_SCRIPT)), redis)
    batch = await read_events(redis, "0-0", block_ms=10)
    await store.record(batch)
    await store.record(batch)  # e.g. the process died after writing but before saving the cursor
    assert len(await store.events("run-test")) == len(batch)


async def test_recorder_resumes_from_its_cursor(redis: FakeRedis, store: EventStore) -> None:
    from agenthq.events import Event, EventType

    bus = RedisBus(redis, "run-a", [])
    for i in range(3):
        await bus.publish(Event(run_id="run-a", type=EventType.MESSAGE, payload={"content": str(i)}))
    await record_everything(redis, store)
    cursor = await store.cursor()
    await bus.publish(Event(run_id="run-a", type=EventType.MESSAGE, payload={"content": "3"}))

    recorder = asyncio.create_task(EventRecorder(redis, store).run_forever())
    async with asyncio.timeout(5):
        while len(await store.events("run-a")) < 4:
            await asyncio.sleep(0.01)
    recorder.cancel()
    assert await store.cursor() != cursor
    assert [e.payload["content"] for e in await store.events("run-a")] == ["0", "1", "2", "3"]


async def test_goal_falls_back_to_the_humans_goal_message(redis: FakeRedis, store: EventStore) -> None:
    from agenthq.events import Event, EventType

    bus = RedisBus(redis, "run-old", ["manager"])
    await bus.publish(Event(run_id="run-old", type=EventType.STATUS_CHANGE, payload={"run_state": "preparing"}))
    await bus.publish(Event(run_id="run-old", type=EventType.MESSAGE, sender="human", recipient="manager",
                            payload={"content": "Goal: Add a health endpoint"}))
    await record_everything(redis, store)
    assert (await store.runs())[0].goal == "Add a health endpoint"
