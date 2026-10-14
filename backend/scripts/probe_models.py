"""Check which models of a provider work with AgentHQ's real tool definitions.

Run from backend/:  python scripts/probe_models.py gemini gemini-2.5-flash gemini-3.1-flash-lite
For each model it makes one call with the Engineer's tools and checks that the model answers with a
valid tool call. Cheap: one short request per model.
"""

import asyncio
import sys

from agenthq.config import get_settings
from agenthq.llm import LLMError, Message, make_llm_client
from agenthq.tools import TOOLS

ENGINEER_TOOLS = ["read_file", "list_files", "search_code", "edit_file", "write_file", "run_tests", "submit_task", "fail_task",
                  "send_message"]


async def main(provider: str, models: list[str]) -> None:
    client = make_llm_client(get_settings(), provider)
    tools = [TOOLS[name].spec() for name in ENGINEER_TOOLS]
    ask = [Message(role="user", content="Read the file app/main.py. Use the read_file tool.")]
    for model in models:
        try:
            response = await client.complete("You are a software engineer. Act only through your tools.", ask, tools, model=model)
            calls = [(c.name, c.arguments) for c in response.tool_calls]
            verdict = "OK" if calls and calls[0][0] == "read_file" else "NO TOOL CALL"
            print(f"{verdict:<13} {model:<28} {calls} [{response.tokens_in} in, {response.tokens_out} out]")
        except LLMError as error:
            print(f"{'ERROR':<13} {model:<28} {str(error)[:300]}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], sys.argv[2:]))
