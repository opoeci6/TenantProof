"""Chat-completions adapters exercise their wire protocol without network calls."""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.services.agent import chat, llm
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
    deadline_s: float = llm.CALL_TIMEOUT_S,
) -> ChatCompletionsProvider:
    return ChatCompletionsProvider(
        SECRET, "gpt-5-mini", provider="openai", base_url=BASE_URL, effort=effort,
        deadline_s=deadline_s, transport=httpx.MockTransport(handler),
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


def test_stop_with_tool_calls_is_accepted() -> None:
    """Gemini's compatibility endpoint reports "stop" for a function-call turn."""
    provider = adapter(lambda _: response({
        "content": None, "tool_calls": [tool_call()],
    }, "stop"))
    result = asyncio.run(provider.complete(request()))
    assert result.stop_reason == "tool_use"
    assert [use.name for use in result.tool_uses] == ["record_fact"]


def test_tool_calls_with_no_calls_still_fail_closed() -> None:
    provider = adapter(lambda _: response({"content": "No calls."}, "tool_calls"))
    with pytest.raises(llm.LLMCallError, match="unsupported completion state"):
        asyncio.run(provider.complete(request()))


def test_chat_output_tokens_are_capped_at_the_provider_safe_limit() -> None:
    payloads: list[dict[str, Any]] = []

    def handler(sent: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(sent.content))
        return response({"content": "ok"})

    asyncio.run(adapter(handler).complete(request()))
    large = request().model_copy(update={"max_tokens": 16000})
    asyncio.run(adapter(handler).complete(large))
    assert chat.CHAT_MAX_OUTPUT_TOKENS == 8000
    assert payloads[0]["max_completion_tokens"] == 1300  # below the cap: unchanged
    assert payloads[1]["max_completion_tokens"] == chat.CHAT_MAX_OUTPUT_TOKENS


def test_provider_status_error_carries_the_message_but_not_the_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": "max_tokens must be <= 8192", "type": "invalid_request_error"},
        })

    with pytest.raises(llm.LLMCallError) as err:
        asyncio.run(adapter(handler).complete(request()))
    text = str(err.value)
    assert "Provider HTTP status 400" in text
    assert "max_tokens must be <= 8192" in text
    assert SECRET not in text


def test_provider_status_error_scrubs_echoed_secret_even_at_truncation_edge() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={
            "error": {"message": ("x" * 190) + f" bad key {SECRET}"},
        })

    with pytest.raises(llm.LLMCallError) as err:
        asyncio.run(adapter(handler).complete(request()))
    text = str(err.value)
    assert "Provider HTTP status 400" in text
    assert SECRET not in text
    assert SECRET[:8] not in text


@pytest.mark.parametrize("code", [401, 403])
def test_provider_auth_status_errors_never_echo_body(code: int) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={
            "error": {"message": f"Incorrect API key provided: {SECRET[:6]}****{SECRET[-4:]}"},
        })

    with pytest.raises(llm.LLMCallError) as err:
        asyncio.run(adapter(handler).complete(request()))
    assert str(err.value) == f"Provider HTTP status {code}"


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


@pytest.mark.parametrize("failure", [429, 500, 503])
def test_retryable_http_response_retries_once_with_the_same_payload(failure: int) -> None:
    payloads: list[bytes] = []

    def handler(sent: httpx.Request) -> httpx.Response:
        payloads.append(sent.content)
        if len(payloads) == 1:
            return httpx.Response(failure, headers={"Retry-After": "0"})
        return response({"content": "Recovered."})

    assert asyncio.run(adapter(handler).complete(request())).text == "Recovered."
    assert len(payloads) == 2 and payloads[0] == payloads[1]


@pytest.mark.parametrize("failure", [429, 500, 503, 400, 401, 403, 404, 408])
def test_retry_limit_and_nonretryable_http_errors(failure: int) -> None:
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(failure, headers={"Retry-After": "0"}, text=SECRET)

    with pytest.raises(llm.LLMCallError, match=str(failure)) as caught:
        asyncio.run(adapter(handler).complete(request()))
    assert SECRET not in str(caught.value)
    assert calls == (2 if failure == 429 or failure >= 500 else 1)


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
def test_connection_failure_retries_once_with_default_backoff(
    monkeypatch: pytest.MonkeyPatch, failure: type[httpx.RequestError],
) -> None:
    calls = 0
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    def handler(sent: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise failure("mock connection failure", request=sent)
        return response({"content": "Recovered."})

    monkeypatch.setattr(chat.asyncio, "sleep", sleep)
    assert asyncio.run(adapter(handler).complete(request())).text == "Recovered."
    assert calls == 2 and waits == [0.5]


@pytest.mark.parametrize("date_header", [False, True])
def test_retry_after_accepts_seconds_and_an_http_date(
    monkeypatch: pytest.MonkeyPatch, date_header: bool,
) -> None:
    delay_s = 20
    retry_after = format_datetime(
        datetime.now(UTC) + timedelta(seconds=delay_s), usegmt=True,
    ) if date_header else str(delay_s)
    calls = 0
    waits: list[float] = []

    async def sleep(delay: float) -> None:
        waits.append(delay)

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return response({"content": "Recovered."})

    monkeypatch.setattr(chat.asyncio, "sleep", sleep)
    assert asyncio.run(adapter(handler).complete(request())).text == "Recovered."
    assert calls == 2 and len(waits) == 1
    if date_header:
        assert 0.5 < waits[0] <= delay_s
    else:
        assert waits == [delay_s]


def test_retry_after_outside_the_budget_does_not_sleep_or_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    async def sleep(_: float) -> None:
        pytest.fail("A Retry-After outside the remaining deadline should never be awaited")

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, headers={"Retry-After": "90"})

    monkeypatch.setattr(chat.asyncio, "sleep", sleep)
    with pytest.raises(llm.LLMCallError, match="429"):
        asyncio.run(adapter(handler, deadline_s=2).complete(request()))
    assert calls == 1


def test_stalled_retry_is_cancelled_by_the_overall_deadline() -> None:
    calls = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, headers={"Retry-After": "0"})
        await asyncio.Event().wait()
        raise AssertionError("Mock request unexpectedly completed")

    async def complete() -> None:
        provider = ChatCompletionsProvider(
            SECRET, "gpt-5-mini", base_url=BASE_URL, deadline_s=0.1,
            transport=httpx.MockTransport(handler),
        )
        await asyncio.wait_for(provider.complete(request()), timeout=2)

    with pytest.raises(llm.LLMTimeout):
        asyncio.run(complete())
    assert calls == 2


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
