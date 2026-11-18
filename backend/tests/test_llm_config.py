"""LLM_MODEL and ANTHROPIC_BASE_URL: validation of the settings, and what the live provider
does with them. No network: the client is built but never called."""

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.agent import llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Some machines set these already; the tests set what they need."""
    for name in ("LLM_MODEL", "ANTHROPIC_BASE_URL", "RUN_BUDGET_S"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-real")


def make(**values: str) -> Settings:
    """Settings from these values only: no .env file, and the environment as the fixture left it."""
    return Settings(_env_file=None, **values)  # type: ignore[call-arg, arg-type]


def provider(**values: str) -> llm.AnthropicProvider:
    built = llm.get_provider(make(llm_mode="live", **values))
    assert isinstance(built, llm.AnthropicProvider)
    return built


# --- the settings -----------------------------------------------------------------------------


def test_both_are_empty_by_default() -> None:
    s = make()
    assert (s.llm_model, s.anthropic_base_url) == ("", "")


def test_the_run_budget_is_180_s_by_default_and_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert make().run_budget_s == 180.0
    monkeypatch.setenv("RUN_BUDGET_S", "240")
    assert make().run_budget_s == 240.0


@pytest.mark.parametrize("value", ["0", "-5", "9.9", "901", "abc"])
def test_a_run_budget_out_of_range_is_refused(value: str) -> None:
    with pytest.raises(ValidationError, match="run_budget_s"):
        make(run_budget_s=value)


def test_they_are_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.com/anthropic/")
    s = make()
    assert (s.llm_model, s.anthropic_base_url) == (
        "deepseek-chat", "https://api.example.com/anthropic")


@pytest.mark.parametrize("url", [
    "https://api.example.com", "https://api.example.com/anthropic/", "http://localhost:8080",
    "http://127.0.0.1:9000/v1", "http://[::1]:9000",
])
def test_https_or_local_http_urls_are_accepted(url: str) -> None:
    assert make(anthropic_base_url=url).anthropic_base_url == url.rstrip("/")


@pytest.mark.parametrize("url", [
    "http://api.example.com", "http://localhost.evil.example", "https://user:pw@api.example.com",
    "https://user@api.example.com", "ftp://api.example.com", "api.example.com", "https://",
    "javascript:alert(1)", "file:///etc/passwd",
])
def test_anything_else_is_refused_so_the_key_and_evidence_cannot_leave_in_clear(url: str) -> None:
    with pytest.raises(ValidationError):
        make(anthropic_base_url=url)


@pytest.mark.parametrize("name", ["claude-sonnet-5-5", "deepseek-chat", "vendor/model:v1.2"])
def test_model_names_are_accepted(name: str) -> None:
    assert make(llm_model=f"  {name} ").llm_model == name


@pytest.mark.parametrize("name", ["two words", "x;rm -rf", "-leading-dash", "a" * 101, "new\nline"])
def test_odd_model_names_are_refused(name: str) -> None:
    with pytest.raises(ValidationError):
        make(llm_model=name)


# --- the provider -----------------------------------------------------------------------------


def test_defaults_give_the_built_in_model_and_anthropics_own_api() -> None:
    p = provider()
    assert p.model == llm.MODEL == "claude-sonnet-5-5"
    assert str(p.client.base_url).rstrip("/") == "https://api.anthropic.com"


@pytest.mark.parametrize("env_value", [None, "", "   "])
def test_an_empty_or_unset_base_url_means_anthropics_own_api(
    monkeypatch: pytest.MonkeyPatch, env_value: str | None
) -> None:
    """Compose passes ANTHROPIC_BASE_URL as an empty string when it is not set. The SDK would
    take that as the address "" and every live call would fail to connect."""
    if env_value is not None:
        monkeypatch.setenv("ANTHROPIC_BASE_URL", env_value)
    p = llm.get_provider(Settings(llm_mode="live", _env_file=None))  # type: ignore[call-arg]
    assert isinstance(p, llm.AnthropicProvider)
    assert str(p.client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_the_provider_does_not_depend_on_the_sdks_own_env_handling(
    monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "")
    direct = llm.AnthropicProvider("k")  # no base_url argument at all
    assert str(direct.client.base_url).rstrip("/") == "https://api.anthropic.com"


def test_the_configured_model_and_base_url_reach_the_client() -> None:
    p = provider(llm_model="deepseek-chat", anthropic_base_url="https://api.example.com/anthropic")
    assert p.model == "deepseek-chat"
    assert str(p.client.base_url).startswith("https://api.example.com/anthropic")


def test_only_the_model_can_be_set_and_only_the_url() -> None:
    assert provider(llm_model="other-model").model == "other-model"
    only_url = provider(anthropic_base_url="http://localhost:8080")
    assert only_url.model == llm.MODEL
    assert str(only_url.client.base_url).startswith("http://localhost:8080")


def test_the_model_is_sent_on_every_request() -> None:
    from types import SimpleNamespace

    class Messages:
        calls: list[dict] = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(content=[], stop_reason="end_turn",
                                   usage=SimpleNamespace(input_tokens=1, output_tokens=1))

    client = SimpleNamespace(messages=Messages())
    p = llm.AnthropicProvider("k", model="deepseek-chat", client=client)
    request = llm.LLMRequest(system="s", messages=[llm.Message(role="user", content="x")],
                             fixture_key="k")
    import asyncio

    asyncio.run(p.complete(request))
    assert client.messages.calls[0]["model"] == "deepseek-chat"


def test_fixture_mode_ignores_both_settings() -> None:
    s = make(llm_mode="fixture", llm_model="x", anthropic_base_url="https://a.example")
    assert isinstance(llm.get_provider(s), llm.FixtureProvider)


def test_the_key_never_appears_in_the_provider_error_for_a_bad_base_url() -> None:
    with pytest.raises(ValidationError) as err:
        make(anthropic_base_url="http://evil.example")
    assert "sk-test-not-real" not in str(err.value)
