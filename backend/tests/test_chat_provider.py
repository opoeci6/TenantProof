"""Chat-completions adapters exercise their wire protocol without network calls."""

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.services.agent import llm
from app.services.agent.chat import ChatCompletionsProvider

BASE_URL = "https://api.example.test/v1"
SECRET = "secret-test-token"
TOOL = {
    "name": "record_fact",
    "description": "Record a fact from the evidence.",
    "input_schema": {
        "type": "object",
        "properties": {"fact": {"type": "string"}},
        "required": ["fact"],
    },
}


def request() -> llm.LLMRequest:
    return llm.LLMRequest(
        system="Stable guidance.",
        system_run="Run guidance.",
        messages=[llm.Message(role="user", content="Read the case.")],
        tools=[TOOL],
        max_tokens=1300,
        fixture_key="chat-test",
    )


def response(
    message: dict[str, Any], finish: str = "stop", *, usage: dict[str, Any] | None = None,
) -> httpx.Response:
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", **message}, "finish_reason": finish}],
        "usage": usage or {},
    })


def tool_call(arguments: str = '{"fact":"Water leak"}') -> dict[str, Any]:
    return {
        "id": "call_fact",
        "type": "function",
        "function": {"name": "record_fact", "arguments": arguments},
    }


def adapter(
    handler: Callable[[httpx.Request], httpx.Response], *, effort: str = "",
) -> ChatCompletionsProvider:
    return ChatCompletionsProvider(
        SECRET, "gpt-5-mini", provider="openai", base_url=BASE_URL, effort=effort,
        transport=httpx.MockTransport(handler),
    )


def test_chat_round_trips_tools_results_and_token_usage() -> None:
    calls: list[dict[str, Any]] = []
    exact_result = r'{"ok":true,"quoted":"漏水\n照片"}'
    thought_signature = {"google": {"thought_signature": "mock-signature"}}

    def handler(sent: httpx.Request) -> httpx.Response:
        assert sent.method == "POST"
        assert str(sent.url) == f"{BASE_URL}/chat/completions"
        assert sent.headers["Authorization"] == f"Bearer {SECRET}"
        calls.append(json.loads(sent.content))
        if len(calls) == 1:
            return response(
                {
                    "content": "I found a fact.",
                    "tool_calls": [{**tool_call(), "extra_content": thought_signature}],
                },
                "tool_calls",
                usage={"prompt_tokens": 20, "completion_tokens": 5,
                       "prompt_tokens_details": {"cached_tokens": 7}},
            )
        return response({"content": "Recorded."})

    async def conversation() -> tuple[llm.LLMResponse, llm.LLMResponse]:
        provider = adapter(handler, effort="high")
        first_request = request()
        first = await provider.complete(first_request)
        tool_result = llm.Message(role="user", content=[{
            "type": "tool_result",
            "tool_use_id": "call_fact",
            "content": exact_result,
        }])
        next_request = first_request.model_copy(update={
            "messages": [*first_request.messages, first.assistant_message(), tool_result],
        })
        return first, await provider.complete(next_request)

    first, final = asyncio.run(conversation())
    assert first.text == "I found a fact." and first.stop_reason == "tool_use"
    assert first.tool_uses == [llm.ToolUse(id="call_fact", name="record_fact",
                                          input={"fact": "Water leak"})]
    assert (first.usage.input_tokens, first.usage.cache_read_input_tokens) == (13, 7)
    assert first.usage.output_tokens == 5 and first.usage.peak_prompt_tokens == 20
    assert final.text == "Recorded." and final.stop_reason == "end_turn"
    assert len(calls) == 2
    assert calls[0]["model"] == "gpt-5-mini" and calls[0]["reasoning_effort"] == "high"
    assert calls[0]["tools"] == [{
        "type": "function",
        "function": {
            "name": TOOL["name"],
            "description": TOOL["description"],
            "parameters": TOOL["input_schema"],
        },
    }]
    instructions = "\n".join(
        msg["content"] for msg in calls[0]["messages"] if msg["role"] in {"system", "developer"}
    )
    assert instructions.index("Stable guidance.") < instructions.index("Run guidance.")
    before, after = calls[1]["messages"][-2:]
    assert before["role"] == "assistant" and before["content"] == "I found a fact."
    restored_call, = before["tool_calls"]
    assert restored_call["id"] == "call_fact"
    assert restored_call["function"]["name"] == "record_fact"
    assert json.loads(restored_call["function"]["arguments"]) == {"fact": "Water leak"}
    assert restored_call["extra_content"] == thought_signature
    assert after == {"role": "tool", "tool_call_id": "call_fact", "content": exact_result}


@pytest.mark.parametrize(("finish", "extra", "stop_reason"), [
    ("length", {}, "max_tokens"),
    ("content_filter", {}, "refusal"),
    ("stop", {"refusal": "No."}, "refusal"),
])
def test_stopped_replies_never_expose_executable_tool_calls(finish, extra, stop_reason) -> None:
    provider = adapter(lambda _: response({
        "content": None, "tool_calls": [tool_call()], **extra,
    }, finish))
    result = asyncio.run(provider.complete(request()))
    assert result.stop_reason == stop_reason
    assert result.tool_uses == []
    assert isinstance(result.content, list)
    assert all(block["type"] != "tool_use" for block in result.content)


@pytest.mark.parametrize("arguments", ['{"fact":', '["not a named arguments object"]'])
def test_invalid_tool_arguments_fail_closed(arguments: str) -> None:
    provider = adapter(lambda _: response({
        "content": None, "tool_calls": [tool_call(arguments)],
    }, "tool_calls"))
    with pytest.raises(llm.LLMCallError):
        asyncio.run(provider.complete(request()))


def test_duplicate_tool_call_identifiers_fail_closed() -> None:
    provider = adapter(lambda _: response({
        "tool_calls": [tool_call(), tool_call()],
    }, "tool_calls"))
    with pytest.raises(llm.LLMCallError, match="duplicate"):
        asyncio.run(provider.complete(request()))


@pytest.mark.parametrize(("provider_name", "expected"), [
    ("openrouter", {"reasoning": {"effort": "high"}}),
    ("deepseek", {"reasoning_effort": "high", "thinking": {"type": "enabled"}}),
])
def test_chat_translates_explicit_effort_for_the_endpoint(provider_name, expected) -> None:
    def handler(sent: httpx.Request) -> httpx.Response:
        payload = json.loads(sent.content)
        assert {key: payload[key] for key in expected} == expected
        return response({"content": "Done."})

    provider = ChatCompletionsProvider(
        SECRET, "explicit-model", provider=provider_name, base_url=BASE_URL, effort="high",
        transport=httpx.MockTransport(handler),
    )
    assert asyncio.run(provider.complete(request())).text == "Done."


def test_redirect_is_not_followed_and_error_keeps_secrets_out() -> None:
    destinations = []

    def handler(sent: httpx.Request) -> httpx.Response:
        destinations.append(str(sent.url))
        return httpx.Response(
            307, headers={"Location": "https://unexpected.example.test/collect"}, text=SECRET,
        )

    with pytest.raises(llm.LLMCallError) as caught:
        asyncio.run(adapter(handler).complete(request()))
    assert destinations == [f"{BASE_URL}/chat/completions"]
    assert SECRET not in str(caught.value)


API_KEYS = [
    ("openai", "OPENAI_API_KEY"),
    ("openrouter", "OPENROUTER_API_KEY"),
    ("groq", "GROQ_API_KEY"),
    ("deepseek", "DEEPSEEK_API_KEY"),
    ("gemini", "GEMINI_API_KEY"),
    ("grok", "XAI_API_KEY"),
    ("openai_compatible", "OPENAI_COMPATIBLE_API_KEY"),
]


@pytest.mark.parametrize(("provider_name", "key_name"), API_KEYS)
def test_live_provider_reads_only_its_own_key_from_the_environment(
    monkeypatch: pytest.MonkeyPatch, provider_name, key_name,
) -> None:
    for _, env_key in API_KEYS:
        monkeypatch.delenv(env_key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unrelated-anthropic-test-key")
    settings = Settings(
        llm_mode="live", llm_provider=provider_name, llm_model="explicit-model",
        llm_effort="",
        openai_compatible_base_url=BASE_URL if provider_name == "openai_compatible" else "",
        _env_file=None,  # type: ignore[call-arg]
    )
    with pytest.raises(llm.LLMConfigError, match=key_name):
        llm.get_provider(settings)
    monkeypatch.setenv(key_name, SECRET)
    provider = llm.get_provider(settings)
    assert isinstance(provider, ChatCompletionsProvider)
    assert provider.model == "explicit-model"
    if provider_name == "openai_compatible":
        assert str(provider.base_url).rstrip("/") == BASE_URL

    def handler(sent: httpx.Request) -> httpx.Response:
        assert sent.headers["Authorization"] == f"Bearer {SECRET}"
        assert "reasoning_effort" not in json.loads(sent.content)
        return response({"content": "Done."})

    provider.transport = httpx.MockTransport(handler)
    assert asyncio.run(provider.complete(request())).text == "Done."


def test_non_anthropic_requires_explicit_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    settings = Settings(
        llm_mode="live", llm_provider="openai", llm_model="", _env_file=None,  # type: ignore[call-arg]
    )
    with pytest.raises(llm.LLMConfigError, match="LLM_MODEL"):
        llm.get_provider(settings)


def test_fixture_provider_needs_no_live_settings_or_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_COMPATIBLE_API_KEY", raising=False)
    settings = Settings(
        llm_mode="fixture", llm_provider="openai_compatible", llm_model="",
        openai_compatible_base_url="", _env_file=None,  # type: ignore[call-arg]
    )
    assert isinstance(llm.get_provider(settings), llm.FixtureProvider)
