import pytest

from agenthq.llm import FakeLLMClient, LLMError, LLMResponse, Message, ToolCall, ToolSpec

TOOLS = [ToolSpec(name="read_file", description="Read a file")]


async def test_returns_script_in_order_and_records_calls() -> None:
    fake = FakeLLMClient([
        LLMResponse(tool_calls=[ToolCall(id="c1", name="read_file", arguments={"path": "app/main.py"})]),
        LLMResponse(text="done"),
    ])
    history = [Message(role="user", content="look at main.py")]

    first = await fake.complete("system", history, TOOLS)
    history.append(first.as_message())
    second = await fake.complete("system", history, TOOLS)

    assert first.tool_calls[0].arguments == {"path": "app/main.py"}
    assert second.text == "done"
    assert len(fake.calls) == 2
    # The first call's recorded history must not change when the caller appends later.
    assert len(fake.calls[0][1]) == 1


async def test_estimates_tokens_when_script_omits_them() -> None:
    fake = FakeLLMClient([LLMResponse(text="x" * 40)])
    response = await fake.complete("s" * 400, [], [])
    assert response.tokens_in == 100
    assert response.tokens_out == 10
    assert response.model == "fake-model"


async def test_keeps_scripted_token_counts() -> None:
    fake = FakeLLMClient([LLMResponse(text="hi", tokens_in=7, tokens_out=3)])
    response = await fake.complete("system", [], [])
    assert (response.tokens_in, response.tokens_out) == (7, 3)


async def test_scripted_error_is_raised() -> None:
    fake = FakeLLMClient([LLMError("rate limited", retryable=True), LLMResponse(text="ok")])
    with pytest.raises(LLMError) as raised:
        await fake.complete("system", [], [])
    assert raised.value.retryable
    assert (await fake.complete("system", [], [])).text == "ok"


async def test_exhausted_script_fails_loudly() -> None:
    fake = FakeLLMClient([])
    with pytest.raises(RuntimeError, match="exhausted"):
        await fake.complete("system", [], [])
