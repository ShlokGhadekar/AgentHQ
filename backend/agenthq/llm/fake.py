"""A scripted LLM for tests and the offline demo: no network, no key, same answers every time."""

from agenthq.llm.base import LLMError, LLMResponse, Message, ToolSpec


class FakeLLMClient:
    """Replays a fixed script of responses, one per call, and records every call it receives.

    Each agent gets its own FakeLLMClient, so a script is just "what this agent says, in order",
    and concurrent agents can't steal each other's responses. A script entry may also be an
    LLMError, which is raised instead of returned, to exercise retry and escalation paths.
    """

    def __init__(self, script: list[LLMResponse | LLMError], model: str = "fake-model") -> None:
        self._script = list(script)
        self._model = model
        self.calls: list[tuple[str, list[Message], list[ToolSpec]]] = []

    async def complete(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec],
        model: str | None = None,
    ) -> LLMResponse:
        """Return (or raise) the next scripted entry."""
        # Copy the list: callers append to their history afterwards, and tests inspect the
        # conversation exactly as it was at call time.
        self.calls.append((system, list(messages), tools))
        if not self._script:
            raise RuntimeError(f"FakeLLMClient script exhausted after {len(self.calls) - 1} calls")
        entry = self._script.pop(0)
        if isinstance(entry, LLMError):
            raise entry
        return self._with_usage(entry, system, messages, model)

    def _with_usage(self, response: LLMResponse, system: str, messages: list[Message], model: str | None) -> LLMResponse:
        """Fill in token counts if the script left them out, so the stats bar has numbers in demo mode.

        Roughly 4 characters per token, the usual rule of thumb for English text.
        """
        update: dict[str, object] = {"model": response.model or model or self._model}
        if response.tokens_in == 0:
            update["tokens_in"] = (len(system) + sum(len(m.content) for m in messages)) // 4
        if response.tokens_out == 0:
            update["tokens_out"] = max(1, (len(response.text) + sum(len(str(c.arguments)) for c in response.tool_calls)) // 4)
        return response.model_copy(update=update)
