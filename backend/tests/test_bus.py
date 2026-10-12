from fakeredis.aioredis import FakeRedis

from agenthq.bus import EVENTS_STREAM, RedisBus, latest_event_id, read_events
from agenthq.events import Event, EventType


def message(sender: str, recipient: str | None, content: str = "hi", run_id: str = "run1") -> Event:
    return Event(run_id=run_id, type=EventType.MESSAGE, sender=sender, recipient=recipient, payload={"content": content})


async def test_message_goes_to_recipient_inbox_and_global_stream(redis: FakeRedis) -> None:
    bus = RedisBus(redis, "run1", ["manager", "engineer"])
    event = message("manager", "engineer")
    await bus.publish(event)

    assert await bus.read_inbox("engineer", block_ms=10) == [event]
    assert await bus.read_inbox("manager", block_ms=10) == []
    assert [e for _, e in await read_events(redis, "0-0", block_ms=10)] == [event]


async def test_events_without_an_agent_recipient_only_go_to_global_stream(redis: FakeRedis) -> None:
    bus = RedisBus(redis, "run1", ["manager"])
    await bus.publish(message("manager", None))
    await bus.publish(message("manager", "human"))  # the human has no inbox; the UI reads the global stream

    assert await redis.xlen(EVENTS_STREAM) == 2
    assert await redis.exists(bus.inbox_key("manager")) == 0


async def test_inbox_cursor_delivers_each_event_once_in_order(redis: FakeRedis) -> None:
    bus = RedisBus(redis, "run1", ["engineer"])
    for text in ["one", "two"]:
        await bus.publish(message("manager", "engineer", text))
    first = await bus.read_inbox("engineer", block_ms=10)
    await bus.publish(message("manager", "engineer", "three"))
    second = await bus.read_inbox("engineer", block_ms=10)

    assert [e.payload["content"] for e in first] == ["one", "two"]
    assert [e.payload["content"] for e in second] == ["three"]


async def test_inboxes_are_separate_per_run(redis: FakeRedis) -> None:
    run1, run2 = RedisBus(redis, "run1", ["engineer"]), RedisBus(redis, "run2", ["engineer"])
    await run1.publish(message("manager", "engineer", run_id="run1"))
    assert await run2.read_inbox("engineer", block_ms=10) == []


async def test_reading_after_latest_id_skips_history(redis: FakeRedis) -> None:
    bus = RedisBus(redis, "run1", [])
    await bus.publish(message("manager", None, "old"))
    start = await latest_event_id(redis)
    await bus.publish(message("manager", None, "new"))
    assert [e.payload["content"] for _, e in await read_events(redis, start, block_ms=10)] == ["new"]
