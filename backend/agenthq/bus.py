"""Message bus on Redis Streams.

Two kinds of stream:
- one inbox per agent per run, holding only the events addressed to that agent;
- one global `agenthq:events` stream holding every event of every run, in order. This is the
  source of truth that the terminal log, the UI and replay read.

A Redis stream is an append-only log with ids, so a reader that remembers the last id it saw
can never miss or double-read an entry, and the history stays readable afterwards (replay).
"""

from collections.abc import Iterable

from redis.asyncio import Redis

from agenthq.events import Event

EVENTS_STREAM = "agenthq:events"


class RedisBus:
    """Publishes a run's events and reads its agents' inboxes."""

    def __init__(self, redis: Redis, run_id: str, inboxes: Iterable[str]) -> None:
        self._redis = redis
        self._run_id = run_id
        # Each inbox has exactly one reader (its agent), so a cursor is all we need. Consumer groups
        # would only pay off with several workers sharing one inbox (e.g. a pool of engineers).
        self._cursors = {name: "0-0" for name in inboxes}

    def inbox_key(self, agent: str) -> str:
        return f"agenthq:run:{self._run_id}:inbox:{agent}"

    async def publish(self, event: Event) -> None:
        """Append to the global stream, and to the recipient's inbox if the recipient is an agent.

        Both writes go in one MULTI transaction, so no reader can ever see an event in an inbox
        that isn't also in the global log (or the other way round).
        """
        fields = {"event": event.model_dump_json()}
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.xadd(EVENTS_STREAM, fields)
            if event.recipient in self._cursors:
                pipe.xadd(self.inbox_key(event.recipient), fields)
            await pipe.execute()

    async def read_inbox(self, agent: str, block_ms: int) -> list[Event]:
        """Return the agent's new events, waiting up to `block_ms` if there are none yet."""
        response = await self._redis.xread({self.inbox_key(agent): self._cursors[agent]}, block=block_ms)
        events = []
        for _stream, entries in response:
            for entry_id, fields in entries:
                self._cursors[agent] = entry_id
                events.append(Event.model_validate_json(fields["event"]))
        return events


async def latest_event_id(redis: Redis) -> str:
    """Id of the newest event in the global stream, so a reader can start right after it."""
    newest = await redis.xrevrange(EVENTS_STREAM, count=1)
    return newest[0][0] if newest else "0-0"


async def read_events(redis: Redis, after_id: str, block_ms: int, count: int | None = None) -> list[tuple[str, Event]]:
    """Events from the global stream after `after_id`, as (stream id, event) pairs (at most `count`)."""
    response = await redis.xread({EVENTS_STREAM: after_id}, block=block_ms, count=count)
    return [(entry_id, Event.model_validate_json(fields["event"])) for _stream, entries in response for entry_id, fields in entries]
