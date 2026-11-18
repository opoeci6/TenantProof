import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import Settings
from app.services.agent import llm
from app.services.agent.tools import tool_specs


def req(key: str = "timeline-turn1", system: str = "sys", **kw: Any) -> llm.LLMRequest:
    return llm.LLMRequest(
        system=system,
        messages=[llm.Message(role="user", content="hi")],
        tools=tool_specs(["finish"]),
        fixture_key=key,
        **kw,
    )


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def write_fixture(directory: Path, request: llm.LLMRequest, text: str = "ok") -> None:
    doc = llm.fixture_document(request, llm.LLMResponse(text=text, usage=llm.Usage(input_tokens=3)))
    (directory / f"{request.fixture_key}.json").write_text(json.dumps(doc), encoding="utf-8")


def test_request_hash_is_12_chars_and_tracks_system_and_tools() -> None:
    a = llm.request_hash(req())
    assert len(a) == 12 and a == llm.request_hash(req(key="other-key"))  # key not hashed
    assert a != llm.request_hash(req(system="changed"))
    other_tools = req().model_copy(update={"tools": tool_specs(["finish", "create_gap"])})
    assert a != llm.request_hash(other_tools)


def test_request_hash_tracks_a_tool_description_and_schema() -> None:
    base = req()
    desc = [{**base.tools[0], "description": "data, no longer 'never instructions'"}]
    schema = [{**base.tools[0], "input_schema": {"type": "object", "properties": {}}}]
    h = llm.request_hash(base)
    assert h != llm.request_hash(base.model_copy(update={"tools": desc}))
    assert h != llm.request_hash(base.model_copy(update={"tools": schema}))


def test_fixture_replays(tmp_path: Path) -> None:
    write_fixture(tmp_path, req(), "replayed")
    res = run(llm.FixtureProvider(tmp_path, strict=True).complete(req()))
    assert res.text == "replayed" and res.usage.input_tokens == 3


def test_hash_mismatch_is_an_error_in_strict_mode(tmp_path: Path) -> None:
    write_fixture(tmp_path, req())
    with pytest.raises(llm.FixtureMismatch, match="re-record"):
        run(llm.FixtureProvider(tmp_path, strict=True).complete(req(system="edited prompt")))


def test_hash_mismatch_warns_otherwise(tmp_path: Path) -> None:
    write_fixture(tmp_path, req(), "stale")
    provider = llm.FixtureProvider(tmp_path, strict=False)
    with pytest.warns(llm.FixtureHashWarning, match="request_hash"):
        res = run(provider.complete(req(system="edited")))
    assert res.text == "stale"


def test_strict_follows_env_and_tests_set_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FIXTURE_STRICT", "1")
    assert llm.FixtureProvider().strict is True
    monkeypatch.delenv("FIXTURE_STRICT")
    assert llm.FixtureProvider().strict is False


def test_missing_fixture(tmp_path: Path) -> None:
    with pytest.raises(llm.FixtureMissing):
        run(llm.FixtureProvider(tmp_path).complete(req("nope")))


@pytest.mark.parametrize("key", ["../evil", "a/b", "a\\b", "..", "", "a b", ".hidden", "x:y"])
def test_fixture_key_cannot_escape_the_directory(tmp_path: Path, key: str) -> None:
    with pytest.raises(llm.LLMError, match="invalid fixture_key"):
        run(llm.FixtureProvider(tmp_path).complete(req(key)))


def test_scripted_responses_skip_files_and_hash() -> None:
    p = llm.FixtureProvider(scripted={"k": llm.LLMResponse(text="scripted")})
    assert run(p.complete(req("k", system="anything"))).text == "scripted"


# --- Anthropic provider against a fake client (no network)


class Block(SimpleNamespace):
    """Fake SDK content block: attributes plus model_dump(), like the real pydantic ones."""

    def model_dump(self, **_: Any) -> dict[str, Any]:
        return dict(vars(self))


class FakeMessages:
    def __init__(self, raw: Any) -> None:
        self.raw, self.calls = raw, []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return self.raw


def fake_client(raw: Any) -> Any:
    return SimpleNamespace(messages=FakeMessages(raw))


def test_anthropic_provider_maps_text_tool_use_and_usage() -> None:
    raw = SimpleNamespace(
        content=[
            Block(type="thinking", thinking="plan it", signature="sig=="),
            Block(type="text", text="thinking "),
            Block(type="text", text="done"),
            Block(type="tool_use", id="tu1", name="finish", input={"summary": "s"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=11, output_tokens=7),
    )
    client = fake_client(raw)
    res = run(llm.AnthropicProvider("k", client=client).complete(req(max_tokens=99)))
    assert res.text == "thinking done" and res.stop_reason == "tool_use"
    assert res.tool_uses == [llm.ToolUse(id="tu1", name="finish", input={"summary": "s"})]
    assert (res.usage.input_tokens, res.usage.output_tokens) == (11, 7)
    sent = client.messages.calls[0]
    assert sent["model"] == "claude-sonnet-5-5" and sent["max_tokens"] == 99
    assert sent["output_config"] == {"effort": "medium"}
    # raw blocks kept in order, thinking included, and sent back unchanged as the assistant turn
    assert [b["type"] for b in res.content] == ["thinking", "text", "text", "tool_use"]
    assert res.content[0] == {"type": "thinking", "thinking": "plan it", "signature": "sig=="}
    assert res.assistant_message() == llm.Message(role="assistant", content=res.content)
    assert sent["tools"][0]["name"] == "finish"
    assert sent["messages"] == [{"role": "user", "content": "hi"}]


def test_default_max_tokens_is_16000() -> None:
    assert llm.LLMRequest(system="s", messages=[], fixture_key="k").max_tokens == 16000


def test_assistant_message_falls_back_to_text_and_tool_uses() -> None:
    res = llm.LLMResponse(
        text="hi", tool_uses=[llm.ToolUse(id="t", name="finish", input={"summary": "s"})]
    )
    assert res.assistant_message().content == [
        {"type": "text", "text": "hi"},
        {"type": "tool_use", "id": "t", "name": "finish", "input": {"summary": "s"}},
    ]


def test_fixture_stores_and_replays_raw_blocks(tmp_path: Path) -> None:
    blocks = [
        {"type": "thinking", "thinking": "t", "signature": "s"},
        {"type": "text", "text": "a"},
    ]
    request = req()
    doc = llm.fixture_document(request, llm.LLMResponse(content=blocks, text="a"))
    (tmp_path / f"{request.fixture_key}.json").write_text(json.dumps(doc), encoding="utf-8")
    res = run(llm.FixtureProvider(tmp_path, strict=True).complete(request))
    assert res.content == blocks and res.assistant_message().content == blocks


def test_refusal_keeps_its_stop_details() -> None:
    details = SimpleNamespace(
        model_dump=lambda **_: {"type": "refusal", "category": "cyber", "explanation": "x"}
    )
    raw = SimpleNamespace(
        content=[], stop_reason="refusal", stop_details=details,
        usage=SimpleNamespace(input_tokens=1, output_tokens=0),
    )  # fmt: skip
    res = run(llm.AnthropicProvider("k", client=fake_client(raw)).complete(req()))
    assert res.stop_reason == "refusal" and (res.stop_details or {})["category"] == "cyber"


@pytest.mark.parametrize(
    ("raw_reason", "mapped"),
    [("pause_turn", "pause_turn"), ("stop_sequence", "other"), ("end_turn", "end_turn")],
)
def test_other_stop_reasons(raw_reason: str, mapped: str) -> None:
    raw = SimpleNamespace(
        content=[], stop_reason=raw_reason, usage=SimpleNamespace(input_tokens=1, output_tokens=0)
    )
    res = run(llm.AnthropicProvider("k", client=fake_client(raw)).complete(req()))
    assert res.stop_reason == mapped and res.stop_details is None


def test_estimated_cost_uses_the_list_price() -> None:
    usage = llm.Usage(input_tokens=1_000_000, output_tokens=500_000)
    assert llm.estimate_cost_usd(usage) == 2.0 + 5.0
    assert llm.estimate_cost_usd(llm.Usage()) == 0.0


def test_anthropic_provider_omits_empty_tools_and_maps_a_refusal() -> None:
    raw = SimpleNamespace(
        content=[], stop_reason="refusal", usage=SimpleNamespace(input_tokens=1, output_tokens=0)
    )
    client = fake_client(raw)
    request = req().model_copy(update={"tools": []})
    res = run(llm.AnthropicProvider("k", client=client).complete(request))
    assert res.stop_reason == "refusal" and "tools" not in client.messages.calls[0]


# --- get_provider / key handling


def test_fixture_mode_never_reads_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert isinstance(llm.get_provider(Settings(llm_mode="fixture")), llm.FixtureProvider)


def test_live_mode_without_a_key_is_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "  ")
    with pytest.raises(llm.LLMConfigError, match="ANTHROPIC_API_KEY"):
        llm.get_provider(Settings(llm_mode="live"))


def test_live_mode_with_a_key_builds_the_anthropic_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")
    provider = llm.get_provider(Settings(llm_mode="live"))
    assert isinstance(provider, llm.AnthropicProvider) and provider.model == "claude-sonnet-5-5"


def test_api_errors_become_llm_call_errors_without_details() -> None:
    import httpx

    class Boom:
        async def create(self, **_: Any) -> Any:
            raise llm.anthropic.APIConnectionError(request=httpx.Request("POST", "http://x/secret"))

    provider = llm.AnthropicProvider("k", client=SimpleNamespace(messages=Boom()))
    with pytest.raises(llm.LLMCallError) as info:
        run(provider.complete(req()))
    assert "APIConnectionError" in str(info.value) and "secret" not in str(info.value)


def test_one_call_is_capped_at_60_s_whatever_the_run_budget() -> None:
    provider = llm.AnthropicProvider("sk-test-not-real")
    assert llm.CALL_TIMEOUT_S == provider.deadline_s == 60.0
    assert provider.client.timeout == llm.CALL_TIMEOUT_S and provider.client.max_retries == 1
    assert not hasattr(llm, "RUN_BUDGET_S")  # the run budget is a setting now (#49)


def test_a_hung_call_hits_the_total_deadline_and_is_not_retried() -> None:
    class Hang:
        calls = 0

        async def create(self, **_: Any) -> Any:
            Hang.calls += 1
            await asyncio.sleep(10)

    provider = llm.AnthropicProvider("k", client=SimpleNamespace(messages=Hang()), deadline_s=0.05)
    with pytest.raises(llm.LLMTimeout):
        run(provider.complete(req()))
    assert Hang.calls == 1


def test_sdk_timeout_error_is_an_llm_timeout() -> None:
    import httpx

    class Slow:
        async def create(self, **_: Any) -> Any:
            raise llm.anthropic.APITimeoutError(request=httpx.Request("POST", "http://x"))

    provider = llm.AnthropicProvider("k", client=SimpleNamespace(messages=Slow()))
    with pytest.raises(llm.LLMTimeout):
        run(provider.complete(req()))
