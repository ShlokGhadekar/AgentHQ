"""LLM client for the Anthropic Messages API (Claude models)."""

from typing import Any

import anthropic

from agenthq.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, retry_after_seconds


class AnthropicClient:
    """Calls the Messages API with tool use."""

    def __init__(self, api_key: str, default_model: str, timeout_s: float, max_tokens: int) -> None:
        # max_retries=0: retries belong to the agent runtime (see OpenAICompatibleClient).
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s, max_retries=0)
        self._default_model = default_model
        self._max_tokens = max_tokens

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str | None = None,
    ) -> LLMResponse:
        """Send the conversation and return the model's next turn."""
        request: dict[str, Any] = {
            "model": model or self._default_model,
            "system": system,
            "messages": to_anthropic_messages(messages),
            "max_tokens": self._max_tokens,
        }
        if tools:
            request["tools"] = to_anthropic_tools(tools)
        try:
            response = await self._client.messages.create(**request)
        except anthropic.APIError as error:
            raise map_anthropic_error(error) from error
        return parse_anthropic_response(response)


def to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate our messages to the Messages API format.

    Anthropic has no "tool" role: tool results are content blocks inside a user turn, and the
    results of parallel tool calls must share one user turn. So consecutive tool messages are
    merged, and so is a new user message that arrives right after tool results (an agent's turn
    often ends with a tool call, and the next inbox message follows it).
    """
    result: list[dict[str, Any]] = []
    for message in messages:
        previous = result[-1] if result else None
        follows_tool_results = previous is not None and previous["role"] == "user" and isinstance(previous["content"], list)
        if message.role == "tool":
            block = {"type": "tool_result", "tool_use_id": message.tool_call_id, "content": message.content}
            if follows_tool_results:
                previous["content"].append(block)
            else:
                result.append({"role": "user", "content": [block]})
        elif message.role == "user" and follows_tool_results:
            previous["content"].append({"type": "text", "text": message.content})
        elif message.role == "assistant" and message.tool_calls:
            blocks: list[dict[str, Any]] = [{"type": "text", "text": message.content}] if message.content else []
            blocks += [{"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments} for call in message.tool_calls]
            result.append({"role": "assistant", "content": blocks})
        else:
            result.append({"role": message.role, "content": message.content})
    return result


def to_anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Translate tool specs to the Messages API format."""
    return [{"name": tool.name, "description": tool.description, "input_schema": tool.parameters} for tool in tools]


def parse_anthropic_response(response: Any) -> LLMResponse:
    """Turn a Messages API response into our LLMResponse."""
    texts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in response.content:
        if block.type == "text":
            texts.append(block.text)
        elif block.type == "tool_use":
            tool_calls.append(ToolCall(id=block.id, name=block.name, arguments=dict(block.input)))
    return LLMResponse(
        text="\n".join(texts),
        tool_calls=tool_calls,
        tokens_in=response.usage.input_tokens,
        tokens_out=response.usage.output_tokens,
        model=response.model,
    )


def map_anthropic_error(error: anthropic.APIError) -> LLMError:
    """Classify an SDK error as retryable or not.

    Network problems, rate limits (429) and server errors (5xx, including 529 "overloaded") pass
    with time; any other 4xx means our request or key is wrong.
    """
    if isinstance(error, anthropic.APIStatusError) and error.status_code == 413:
        return LLMError(f"Request too large: {error}", retryable=True, too_large=True)
    transient_status = isinstance(error, anthropic.APIStatusError) and (error.status_code == 429 or error.status_code >= 500)
    if transient_status or isinstance(error, anthropic.APIConnectionError):  # APITimeoutError is a subclass
        wait = retry_after_seconds(error.response.headers) if isinstance(error, anthropic.APIStatusError) else None
        return LLMError(f"Temporary LLM failure: {error}", retryable=True, retry_after_s=wait)
    return LLMError(f"LLM request failed: {error}", retryable=False)
