"""Manual check that the configured LLM provider works with tool calling.

Run from backend/:  python scripts/llm_smoke.py
It makes two real (cheap) calls: one where the model should call a tool, and one where it
reads the tool result and answers.
"""

import asyncio

from agenthq.config import get_settings
from agenthq.llm import Message, ToolSpec, make_llm_client

WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


async def main() -> None:
    settings = get_settings()
    client = make_llm_client(settings)
    system = "You are a helpful assistant. Use tools when they help."
    history = [Message(role="user", content="What's the weather in Pune right now?")]

    first = await client.complete(system, history, [WEATHER_TOOL])
    print(f"[{settings.llm_provider} / {first.model}] tokens in={first.tokens_in} out={first.tokens_out}")
    print("tool calls:", [(call.name, call.arguments) for call in first.tool_calls])
    if not first.tool_calls:
        print("The model answered without calling the tool:", first.text)
        return

    history.append(first.as_message())
    for call in first.tool_calls:
        history.append(Message(role="tool", tool_call_id=call.id, content="31°C and sunny"))
    second = await client.complete(system, history, [WEATHER_TOOL])
    print("final answer:", second.text)


if __name__ == "__main__":
    asyncio.run(main())
