"""The single Event model. Every agent message, tool call, status change and token count is an Event.

The global event stream is the source of truth: the live UI, replay and eval metrics are all
built by reading events, so this model is the contract between every part of the system.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class EventType(StrEnum):
    """Everything that can happen during a run."""

    TASK_CREATED = "task_created"
    TASK_ASSIGNED = "task_assigned"
    MESSAGE = "message"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    STATUS_CHANGE = "status_change"
    APPROVAL_REQUESTED = "approval_requested"
    APPROVAL_DECISION = "approval_decision"
    LLM_USAGE = "llm_usage"
    INDEXING_PROGRESS = "indexing_progress"
    ERROR = "error"
    RUN_COMPLETE = "run_complete"


class Event(BaseModel):
    """One immutable fact about a run.

    `sender` and `recipient` are agent names ("manager", "engineer", ...) or the special
    actors "human" and "system". `payload` holds the type-specific details, e.g. the message
    text, tool arguments or a status value; keeping it a free-form dict means adding a new
    detail never requires a schema migration.
    """

    id: str = Field(default_factory=lambda: uuid4().hex)
    run_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    type: EventType
    sender: str | None = None
    recipient: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
