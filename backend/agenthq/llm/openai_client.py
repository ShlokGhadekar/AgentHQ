"""LLM client for OpenAI-compatible chat APIs: Groq, Cerebras and Gemini (and OpenAI itself).

They all expose the OpenAI Chat Completions format, so one client covers them by changing the base URL.
"""

import json
import re
from typing import Any

import openai

from agenthq.llm.base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, retry_after_seconds

# Groq parses the model's output server-side and answers 400 with these codes when the model
# produced something unusable (a call to a tool it doesn't have, broken JSON, prose where a tool
# call was expected). The request itself was fine, so a fresh sample usually works.
BAD_SAMPLE_CODES = {"tool_use_failed", "output_parse_failed"}


class OpenAICompatibleClient:
    """Calls a Chat Completions endpoint with function calling."""

    def __init__(self, api_key: str, default_model: str, timeout_s: float, max_tokens: int, base_url: str | None = None) -> None:
        # max_retries=0: retries are the agent runtime's job, so they are visible as events and
        # counted against the run's limits instead of hidden inside the SDK.
        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout_s, max_retries=0)
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
            "messages": to_openai_messages(system, messages),
            "max_tokens": self._max_tokens,
        }
        # Some providers reject an empty tools list, so only send it when there are tools.
        if tools:
            request["tools"] = to_openai_tools(tools)
        try:
            response = await self._client.chat.completions.create(**request)
        except openai.APIError as error:
            raise map_openai_error(error) from error
        return parse_openai_response(response)


def to_openai_messages(system: str, messages: list[Message]) -> list[dict[str, Any]]:
    """Translate our messages to the Chat Completions format."""
    result: list[dict[str, Any]] = [{"role": "system", "content": system}]
    for message in messages:
        if message.role == "tool":
            result.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content})
        elif message.role == "assistant" and message.tool_calls:
            result.append({
                "role": "assistant",
                "content": message.content or None,
                "tool_calls": [
                    {"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                     **(call.provider_data or {})}
                    for call in message.tool_calls
                ],
            })
        else:
            result.append({"role": message.role, "content": message.content})
    return result


def to_openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    """Translate tool specs to the function-calling format."""
    return [
        {"type": "function", "function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters}}
        for tool in tools
    ]


def parse_openai_response(response: Any) -> LLMResponse:
    """Turn a ChatCompletion into our LLMResponse."""
    message = response.choices[0].message
    tool_calls = []
    for call in message.tool_calls or []:
        try:
            arguments = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError as error:
            # Open-weight models occasionally emit broken JSON; a fresh sample usually fixes it.
            raise LLMError(f"Model returned invalid JSON arguments for tool {call.function.name}", retryable=True) from error
        # Anything beyond the standard fields (e.g. Gemini's extra_content) is kept to be sent back.
        tool_calls.append(ToolCall(id=call.id, name=call.function.name, arguments=arguments,
                                   provider_data=dict(call.model_extra) if call.model_extra else None))
    usage = response.usage
    return LLMResponse(
        text=message.content or "",
        tool_calls=tool_calls,
        tokens_in=usage.prompt_tokens if usage else 0,
        tokens_out=usage.completion_tokens if usage else 0,
        model=response.model,
    )


def map_openai_error(error: openai.APIError) -> LLMError:
    """Classify an SDK error as retryable or not.

    Network problems, rate limits (429) and server errors (5xx) pass with time; any other 4xx
    means our request or key is wrong.
    """
    if isinstance(error, openai.APIStatusError) and error.status_code == 413:
        return LLMError(f"Request too large: {error}", retryable=True, too_large=True)
    transient_status = isinstance(error, openai.APIStatusError) and (error.status_code == 429 or error.status_code >= 500)
    if getattr(error, "code", None) in BAD_SAMPLE_CODES:
        return LLMError(f"Model produced unusable output: {error}", retryable=True)
    if transient_status or isinstance(error, openai.APIConnectionError):  # APITimeoutError is a subclass
        wait = retry_after_seconds(error.response.headers) if isinstance(error, openai.APIStatusError) else None
        if wait is None:
            # Gemini puts the wait in the body ("retryDelay": "52649s") instead of a Retry-After header.
            delay = re.search(r"retryDelay['\"]?:\s*['\"](\d+(?:\.\d+)?)s", f"{getattr(error, 'body', '')} {error}")
            wait = float(delay.group(1)) if delay else None
        return LLMError(f"Temporary LLM failure: {error}", retryable=True, retry_after_s=wait)
    return LLMError(f"LLM request failed: {error}", retryable=False)
