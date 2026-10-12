"""Translation between our types and each provider's format, tested offline with real SDK types."""

import anthropic
import httpx
import openai
import pytest
from anthropic.types import Message as AnthropicMessage
from openai.types.chat import ChatCompletion

from agenthq.config import Settings
from agenthq.llm import LLMError, Message, ToolCall, ToolSpec, make_llm_client
from agenthq.llm.anthropic_client import map_anthropic_error, parse_anthropic_response, to_anthropic_messages
from agenthq.llm.openai_client import map_openai_error, parse_openai_response, to_openai_messages, to_openai_tools

# A conversation with a parallel tool call: two tool results follow one assistant turn.
CONVERSATION = [
    Message(role="user", content="Where is login handled?"),
    Message(role="assistant", content="Let me look.", tool_calls=[
        ToolCall(id="c1", name="read_file", arguments={"path": "app/auth.py"}),
        ToolCall(id="c2", name="read_file", arguments={"path": "app/main.py"}),
    ]),
    Message(role="tool", tool_call_id="c1", content="def login(): ..."),
    Message(role="tool", tool_call_id="c2", content="app = FastAPI()"),
]


# --- OpenAI-compatible (Groq) -------------------------------------------------

def test_openai_messages() -> None:
    messages = to_openai_messages("be helpful", CONVERSATION)
    assert messages[0] == {"role": "system", "content": "be helpful"}
    assert messages[2]["tool_calls"][0]["function"] == {"name": "read_file", "arguments": '{"path": "app/auth.py"}'}
    assert messages[3] == {"role": "tool", "tool_call_id": "c1", "content": "def login(): ..."}
    assert len(messages) == 5


def test_openai_tools() -> None:
    tool = ToolSpec(name="grep", description="Search", parameters={"type": "object", "properties": {"q": {"type": "string"}}})
    assert to_openai_tools([tool]) == [{"type": "function", "function": {
        "name": "grep", "description": "Search", "parameters": tool.parameters}}]


def _completion(arguments: str) -> ChatCompletion:
    return ChatCompletion.model_validate({
        "id": "x", "object": "chat.completion", "created": 0, "model": "openai/gpt-oss-120b",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c9", "type": "function", "function": {"name": "read_file", "arguments": arguments}}],
        }}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 12, "total_tokens": 62},
    })


def test_openai_response_parsing() -> None:
    response = parse_openai_response(_completion('{"path": "README.md"}'))
    assert response.text == ""
    assert response.tool_calls == [ToolCall(id="c9", name="read_file", arguments={"path": "README.md"})]
    assert (response.tokens_in, response.tokens_out, response.model) == (50, 12, "openai/gpt-oss-120b")


def test_openai_invalid_tool_json_is_retryable() -> None:
    with pytest.raises(LLMError) as raised:
        parse_openai_response(_completion('{"path": '))
    assert raised.value.retryable


def _http_response(status: int) -> httpx.Response:
    return httpx.Response(status, request=httpx.Request("POST", "https://example.test"))


@pytest.mark.parametrize(("error", "retryable"), [
    (openai.RateLimitError("slow down", response=_http_response(429), body=None), True),
    (openai.InternalServerError("boom", response=_http_response(503), body=None), True),
    (openai.APITimeoutError(request=httpx.Request("POST", "https://example.test")), True),
    (openai.AuthenticationError("bad key", response=_http_response(401), body=None), False),
    (openai.BadRequestError("bad request", response=_http_response(400), body=None), False),
])
def test_openai_error_mapping(error: openai.APIError, retryable: bool) -> None:
    assert map_openai_error(error).retryable is retryable


# --- Anthropic ----------------------------------------------------------------

def test_anthropic_messages_merge_parallel_tool_results() -> None:
    messages = to_anthropic_messages(CONVERSATION)
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[1]["content"][0] == {"type": "text", "text": "Let me look."}
    assert messages[1]["content"][1] == {"type": "tool_use", "id": "c1", "name": "read_file", "input": {"path": "app/auth.py"}}
    assert [block["tool_use_id"] for block in messages[2]["content"]] == ["c1", "c2"]


def test_anthropic_user_message_after_tool_results_joins_that_turn() -> None:
    # An agent's turn often ends with a tool call; the next inbox message must not create a
    # second user turn in a row.
    messages = to_anthropic_messages([*CONVERSATION, Message(role="user", content="Message from engineer: thanks")])
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[2]["content"][-1] == {"type": "text", "text": "Message from engineer: thanks"}


def test_anthropic_response_parsing() -> None:
    raw = AnthropicMessage.model_validate({
        "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
        "stop_reason": "tool_use", "stop_sequence": None,
        "content": [
            {"type": "text", "text": "Reading it."},
            {"type": "tool_use", "id": "t1", "name": "read_file", "input": {"path": "app/auth.py"}},
        ],
        "usage": {"input_tokens": 80, "output_tokens": 20},
    })
    response = parse_anthropic_response(raw)
    assert response.text == "Reading it."
    assert response.tool_calls == [ToolCall(id="t1", name="read_file", arguments={"path": "app/auth.py"})]
    assert (response.tokens_in, response.tokens_out) == (80, 20)


@pytest.mark.parametrize(("status", "retryable"), [(429, True), (500, True), (529, True), (400, False), (401, False)])
def test_anthropic_error_mapping(status: int, retryable: bool) -> None:
    error = anthropic.APIStatusError("error", response=_http_response(status), body=None)
    assert map_anthropic_error(error).retryable is retryable


# --- Factory ------------------------------------------------------------------

def test_factory_fails_fast_without_key() -> None:
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        make_llm_client(Settings(_env_file=None, llm_provider="groq", groq_api_key=None))
