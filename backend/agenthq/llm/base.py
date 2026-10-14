"""Provider-neutral types for talking to an LLM with tool calling.

Agents only ever see these types. Each provider client translates them to and from its own
API format, so switching between Groq and Anthropic is a config change, not a code change.
"""

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field


class ToolSpec(BaseModel):
    """A tool the model may call. `parameters` is a JSON Schema object."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=lambda: {"type": "object", "properties": {}})


class ToolCall(BaseModel):
    """One tool call requested by the model. `id` links it to the matching tool result."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    # Extra fields a provider attached to the call, sent back unchanged in the next request.
    # Gemini 3 requires this: each call carries a "thought signature" it must get back.
    provider_data: dict[str, Any] | None = None


class Message(BaseModel):
    """One turn of a conversation.

    - user: text from the runtime (a task, an inbox message).
    - assistant: the model's reply, possibly with tool calls.
    - tool: the result of one tool call, linked by `tool_call_id`.
    """

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None


class LLMResponse(BaseModel):
    """What the model returned, plus the token counts we bill and cap on."""

    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = ""

    def as_message(self) -> Message:
        """The assistant turn to append to the conversation history."""
        return Message(role="assistant", content=self.text, tool_calls=self.tool_calls)


class LLMError(Exception):
    """An LLM call failed.

    `retryable` tells the caller whether trying again can help: rate limits, timeouts and
    server errors can; a rejected key or a malformed request will fail the same way again.
    """

    def __init__(self, message: str, retryable: bool, retry_after_s: float | None = None, too_large: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable
        # How long the provider asked us to wait (Retry-After header), if it said.
        self.retry_after_s = retry_after_s
        # The request was too big for the provider (HTTP 413): retry only with a shorter conversation.
        self.too_large = too_large


def retry_after_seconds(headers: Mapping[str, str]) -> float | None:
    """Read a Retry-After header given in seconds (what Groq and Anthropic send on 429)."""
    value = headers.get("retry-after")
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None  # an HTTP date instead of seconds; fall back to our own backoff


class LLMClient(Protocol):
    """The one method every provider implements."""

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str | None = None,
    ) -> LLMResponse:
        """Send the conversation and return the model's next turn.

        Raises LLMError on failure. `model` overrides the client's default for this call,
        which is how each agent uses the model configured for it in agents.yaml.
        """
        ...
