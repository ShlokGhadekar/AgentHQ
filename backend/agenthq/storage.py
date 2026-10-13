"""Postgres history: a copy of the event stream, plus a `runs` table built from it.

The Redis stream stays the live source of truth. One recorder (EventRecorder) follows it and
writes every event to Postgres, so runs survive restarts and can be replayed. The recorder is the
only writer: the `runs` table is derived from events too (the first event carries the goal, the
last one the report), and tasks and token usage are computed from events on demand, by the same
logic the live view uses, instead of being stored a second time.
"""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import JSON, BigInteger, Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agenthq.bus import read_events
from agenthq.events import Event, EventType

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


class RunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    goal: Mapped[str | None] = mapped_column(Text)
    repo: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed: Mapped[bool] = mapped_column(Boolean, default=False)
    report: Mapped[str | None] = mapped_column(Text)


class EventRecord(Base):
    __tablename__ = "events"

    # Insertion order = stream order, so ORDER BY seq replays a run exactly as it happened.
    seq: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(32), unique=True)
    run_id: Mapped[str] = mapped_column(String(32), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    type: Mapped[str] = mapped_column(String(32))
    sender: Mapped[str | None] = mapped_column(String(64))
    recipient: Mapped[str | None] = mapped_column(String(64))
    task_id: Mapped[str | None] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON().with_variant(postgresql.JSONB(), "postgresql"))
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)


class StreamCursor(Base):
    """Where the recorder stopped reading the Redis stream, so a restart resumes instead of starting over."""

    __tablename__ = "stream_cursor"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    last_id: Mapped[str] = mapped_column(String(32))


class EventStore:
    """Reads and writes run history. Works on Postgres, and on SQLite in tests."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)
        dialect = postgresql if engine.dialect.name == "postgresql" else sqlite
        self._insert = dialect.insert  # both support ON CONFLICT DO NOTHING

    async def create_tables(self) -> None:
        # No migration tool yet: the schema is created once and hasn't changed. Alembic would come
        # in with the first change to an existing table.
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def record(self, entries: list[tuple[str, Event]]) -> None:
        """Store a batch of stream entries and move the cursor past them, in one transaction.

        Inserting an event that is already stored does nothing, so a batch that is recorded twice
        (the process died after writing but before moving the cursor) is harmless.
        """
        if not entries:
            return
        async with self._sessions.begin() as session:
            for _stream_id, event in entries:
                await session.execute(self._insert(EventRecord).values(
                    event_id=event.id, run_id=event.run_id, timestamp=event.timestamp, type=event.type,
                    sender=event.sender, recipient=event.recipient, task_id=event.task_id,
                    payload=event.model_dump(mode="json")["payload"], tokens_in=event.tokens_in, tokens_out=event.tokens_out,
                ).on_conflict_do_nothing(index_elements=["event_id"]))
                await self._project_run(session, event)
            last_id = entries[-1][0]
            await session.execute(self._insert(StreamCursor).values(name="recorder", last_id=last_id)
                                  .on_conflict_do_update(index_elements=["name"], set_={"last_id": last_id}))

    async def _project_run(self, session: Any, event: Event) -> None:
        """Keep the runs table in step with the events: created on the first event, closed by run_complete."""
        await session.execute(self._insert(RunRecord).values(
            id=event.run_id, goal=event.payload.get("goal"), repo=event.payload.get("repo"), created_at=event.timestamp,
        ).on_conflict_do_nothing(index_elements=["id"]))
        run = await session.get(RunRecord, event.run_id)
        if event.payload.get("goal") and not run.goal:
            run.goal, run.repo = event.payload["goal"], event.payload.get("repo")
        elif not run.goal and event.sender == "human" and str(event.payload.get("content", "")).startswith("Goal: "):
            # Runs recorded before the first event carried the goal still start with the human's goal message.
            run.goal = event.payload["content"].removeprefix("Goal: ")
        if event.type == EventType.RUN_COMPLETE:
            run.finished_at = event.timestamp
            run.failed = bool(event.payload.get("failed"))
            run.report = event.payload.get("report")

    async def cursor(self) -> str:
        async with self._sessions() as session:
            row = await session.get(StreamCursor, "recorder")
            return row.last_id if row else "0-0"

    async def runs(self) -> list[RunRecord]:
        async with self._sessions() as session:
            return list((await session.scalars(select(RunRecord).order_by(RunRecord.created_at))).all())

    async def events(self, run_id: str) -> list[Event]:
        async with self._sessions() as session:
            rows = await session.scalars(select(EventRecord).where(EventRecord.run_id == run_id).order_by(EventRecord.seq))
            return [_to_event(row) for row in rows]


def _to_event(row: EventRecord) -> Event:
    # Events are always stamped in UTC. Postgres keeps the zone; SQLite drops it, so put it back.
    timestamp = row.timestamp if row.timestamp.tzinfo else row.timestamp.replace(tzinfo=UTC)
    return Event(id=row.event_id, run_id=row.run_id, timestamp=timestamp, type=row.type, sender=row.sender,
                 recipient=row.recipient, task_id=row.task_id, payload=row.payload, tokens_in=row.tokens_in,
                 tokens_out=row.tokens_out)


class EventRecorder:
    """Follows the global Redis stream and copies it into Postgres.

    At-least-once delivery (resume from the saved cursor) plus idempotent inserts gives each
    event exactly one row. If Postgres is down, the recorder waits and tries the same batch again;
    nothing is lost because Redis keeps the stream.
    """

    def __init__(self, redis: Redis, store: EventStore, batch_size: int = 500) -> None:
        self._redis = redis
        self._store = store
        self._batch_size = batch_size

    async def run_forever(self) -> None:
        cursor = await self._store.cursor()
        while True:
            entries = await read_events(self._redis, cursor, block_ms=1000, count=self._batch_size)
            if not entries:
                continue
            try:
                await self._store.record(entries)
            except Exception:
                log.exception("Recording %d events failed; retrying in 5 s", len(entries))
                await asyncio.sleep(5)
                continue
            cursor = entries[-1][0]
