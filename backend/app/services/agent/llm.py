"""LLM provider protocol, the Anthropic provider and the fixture provider.

Tests and CI use `FixtureProvider` only and never reach the network. The API key is read from
the environment in one place (`_live_api_key`), and only in live mode. TODO (schema: PR):
`anthropic_api_key`, `llm_model` and `record_fixtures` move into `config.py`.
"""

import asyncio
import hashlib
import json
import os
import re
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

import anthropic
from pydantic import BaseModel, Field

from app.config import Settings

MODEL = "claude-sonnet-5-5"
ANTHROPIC_API_URL = "https://api.anthropic.com"  # used when ANTHROPIC_BASE_URL is empty or unset
RUN_BUDGET_S = 60.0  # the run target
# One total deadline per call (asyncio.timeout), equal to the run budget. The httpx timeout
# (per phase) is set to the same value, so a timed-out attempt has used the whole budget and
# the SDK retry cannot start another one. Retries stay for 429 and 5xx, inside the deadline.
CALL_TIMEOUT_S = RUN_BUDGET_S
MAX_RETRIES = 1
EFFORT = "medium"  # output_config.effort: suggested start for multistep tool use
CACHE_WRITE_PRICE_FACTOR = 1.25  # of the input price: writing a 5-minute cache entry
CACHE_CONTROL = {"type": "ephemeral"}  # the default 5-minute lifetime; a run lasts under 60 s
DEFAULT_MAX_TOKENS = 16000  # thinking counts against max_tokens
FIXTURES_DIR = Path(__file__).parent / "fixtures"
_EVIDENCE_TAG = re.compile(r"evidence-[0-9a-f]{6,}")
_FIXTURE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,150}$")


class LLMError(Exception):
    pass


class LLMConfigError(LLMError):
    """Live mode without a usable setup (for example no API key)."""


class LLMCallError(LLMError):
    """The provider call failed. Carries the error type and HTTP status only."""


class LLMTimeout(LLMCallError):
    """The call hit its total deadline. Not retried."""


class FixtureMissing(LLMError):
    pass


class FixtureMismatch(LLMError):
    """The recorded request_hash differs from the request (strict mode)."""


class FixtureHashWarning(UserWarning):
    """Same, outside strict mode."""


# --------------------------------------------------------------------------- data types


class Message(BaseModel):
    role: Literal["user", "assistant"]
    content: str | list[dict[str, Any]]


class ToolUse(BaseModel):
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class Price:
    """List price in USD per million tokens. Cache writes cost CACHE_WRITE_PRICE_FACTOR times the
    input price; the cache read price differs by model (0.1, 0.05 or 0.025 times it).
    `long_prompt_tokens`: above this many prompt tokens in one request the model has a second,
    dearer rate card that this table does not carry, so such a run has no estimate."""

    in_per_mtok: float
    out_per_mtok: float
    cache_read_per_mtok: float
    long_prompt_tokens: int | None = None


# Anthropic list prices, 2026-11-03. Add a model here only with its published prices. A model that
# is not listed gets no estimate (None), never another model's price.
PRICES: dict[str, Price] = {
    "claude-haiku-5-5": Price(0.10, 0.50, 0.01, long_prompt_tokens=100_000),
    "claude-sonnet-5-5": Price(2.0, 10.0, 0.10),
    "claude-opus-5-5": Price(4.0, 20.0, 0.20),
    "claude-fable-5-2": Price(8.0, 40.0, 0.20),
}


class Usage(BaseModel):
    # As the API reports them: `input_tokens` is only the input that was neither read from nor
    # written to the prompt cache; the two cache counts are separate.
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    # The largest prompt (input plus cache tokens) of any single request counted here, for the
    # long-prompt rate card. A sum of requests would overstate it.
    max_prompt_tokens: int = 0

    @property
    def peak_prompt_tokens(self) -> int:
        own = self.input_tokens + self.cache_creation_input_tokens + self.cache_read_input_tokens
        return self.max_prompt_tokens or own

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            max_prompt_tokens=max(self.peak_prompt_tokens, other.peak_prompt_tokens),
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_creation_input_tokens=(
                self.cache_creation_input_tokens + other.cache_creation_input_tokens
            ),
            cache_read_input_tokens=self.cache_read_input_tokens + other.cache_read_input_tokens,
        )


class LLMRequest(BaseModel):
    # The stable system prompt: identical for every run, so the provider can cache it. Nothing
    # per-run goes here.
    system: str
    # Per-run text (the evidence nonce). The provider puts it after the cache breakpoint, so a
    # new nonce does not invalidate the cached prefix.
    system_run: str = ""
    messages: list[Message]
    tools: list[dict[str, Any]] = Field(default_factory=list)  # tools.tool_specs() output
    max_tokens: int = Field(default=DEFAULT_MAX_TOKENS, ge=1)
    # Names the recorded response. Chosen by the caller, for example
    # "extract-<sha256>-<prompt_version>" or "timeline-turn3". Letters, digits, `_`, `.`, `-`.
    fixture_key: str


class LLMResponse(BaseModel):
    # The raw content blocks (block.model_dump()), thinking blocks included, in order. The loop
    # sends them back unchanged as the assistant turn (`assistant_message`). `text` and
    # `tool_uses` are views of them for the runner's convenience.
    content: list[dict[str, Any]] = Field(default_factory=list)
    text: str = ""
    tool_uses: list[ToolUse] = Field(default_factory=list)
    stop_reason: Literal[
        "end_turn", "tool_use", "max_tokens", "refusal", "pause_turn", "other"
    ] = "end_turn"
    # Set on "refusal": {"type": "refusal", "category": "cyber" | "bio" | ... | None,
    # "explanation": ...}. The runner shows the category only.
    stop_details: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=Usage)

    def assistant_message(self) -> Message:
        """The assistant turn to append to the conversation. Uses the raw blocks; a response
        built by hand without them (scripted tests) falls back to its text and tool_uses."""
        blocks = self.content or [
            *([{"type": "text", "text": self.text}] if self.text else []),
            *({"type": "tool_use", "id": u.id, "name": u.name, "input": u.input}
              for u in self.tool_uses),
        ]  # fmt: skip
        return Message(role="assistant", content=blocks)


class LLMProvider(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


# --------------------------------------------------------------------------- fixture hashing


def request_hash(request: LLMRequest) -> str:
    """12 hex chars over the system prompt (the run's evidence nonce masked) and each tool's
    name, description and schema. It changes when a prompt or a tool definition changes, so
    stale fixtures are noticed."""
    tools = sorted(
        (
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "input_schema": t.get("input_schema"),
            }
            for t in request.tools
        ),
        key=lambda t: str(t["name"]),
    )
    # The evidence tag carries a per-run random suffix; it must not change the hash.
    system = _EVIDENCE_TAG.sub("evidence-NONCE", request.system)
    run_part = _EVIDENCE_TAG.sub("evidence-NONCE", request.system_run)
    blob = json.dumps({"system": system, "system_run": run_part, "tools": tools}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def fixture_document(request: LLMRequest, response: LLMResponse) -> dict[str, Any]:
    """The JSON stored in a fixture file."""
    return {"request_hash": request_hash(request), "response": response.model_dump(mode="json")}


def _fixture_path(directory: Path, key: str) -> Path:
    if not _FIXTURE_KEY.fullmatch(key) or ".." in key:
        raise LLMError(f"invalid fixture_key {key!r}")
    return directory / f"{key}.json"


# --------------------------------------------------------------------------- providers


class FixtureProvider:
    """Replays recorded responses. Never touches the network.

    `strict` (default: env FIXTURE_STRICT=1, which tests set) turns a request_hash mismatch
    into an error. Otherwise it warns (FixtureHashWarning) and replays anyway. `scripted`
    entries are in-memory responses for unit tests and skip the hash check.
    """

    def __init__(
        self,
        directory: Path | None = None,
        *,
        strict: bool | None = None,
        scripted: dict[str, LLMResponse] | None = None,
    ) -> None:
        self.directory = directory or FIXTURES_DIR
        self.strict = os.environ.get("FIXTURE_STRICT") == "1" if strict is None else strict
        self.scripted = scripted or {}

    async def complete(self, request: LLMRequest) -> LLMResponse:
        if request.fixture_key in self.scripted:
            return self.scripted[request.fixture_key]
        path = _fixture_path(self.directory, request.fixture_key)
        if not path.is_file():
            raise FixtureMissing(f"no fixture for key {request.fixture_key!r} ({path.name})")
        doc = json.loads(path.read_text(encoding="utf-8"))
        self._check_hash(request, doc.get("request_hash"))
        return LLMResponse.model_validate(doc["response"])

    def _check_hash(self, request: LLMRequest, recorded: str | None) -> None:
        current = request_hash(request)
        if recorded == current:
            return
        msg = (
            f"fixture {request.fixture_key!r} was recorded for request_hash {recorded}, "
            f"this request is {current}: the prompt or tool schemas changed, re-record it"
        )
        if self.strict:
            raise FixtureMismatch(msg)
        warnings.warn(msg, FixtureHashWarning, stacklevel=3)


_STOP_REASONS: dict[str, Literal["end_turn", "tool_use", "max_tokens", "refusal", "pause_turn"]] = {
    "end_turn": "end_turn",
    "tool_use": "tool_use",
    "max_tokens": "max_tokens",
    "refusal": "refusal",
    "pause_turn": "pause_turn",
}


class AnthropicProvider:
    """Live provider. `client` is injectable so tests use a fake and never reach the network."""

    def __init__(
        self,
        api_key: str,
        model: str = MODEL,
        client: Any | None = None,
        effort: str = EFFORT,
        deadline_s: float = RUN_BUDGET_S,
        base_url: str | None = None,
    ) -> None:
        """`base_url` points the client at an Anthropic-compatible API (empty or None:
        Anthropic's own). It is always passed to the client: the SDK reads ANTHROPIC_BASE_URL
        itself and treats an empty value (which compose passes when it is unset) as the address
        "", so every call fails to connect. The key and the evidence text go to that host."""
        self.model = model
        self.effort = effort
        self.deadline_s = deadline_s
        self.client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=CALL_TIMEOUT_S, max_retries=MAX_RETRIES,
            base_url=base_url or ANTHROPIC_API_URL,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": cached_system(request),
            "messages": [m.model_dump(mode="json") for m in request.messages],
            "output_config": {"effort": self.effort},
        }
        if request.tools:
            kwargs["tools"] = cached_tools(request.tools)
        try:
            async with asyncio.timeout(self.deadline_s):
                raw = await self.client.messages.create(**kwargs)
        except TimeoutError:
            raise LLMTimeout(f"no response within {self.deadline_s:.0f}s") from None
        except anthropic.APITimeoutError:
            raise LLMTimeout("provider request timed out") from None
        except anthropic.APIError as exc:
            status = getattr(exc, "status_code", None)
            raise LLMCallError(f"{type(exc).__name__} (status {status})") from None
        return _to_response(raw)


def cached_system(request: LLMRequest) -> list[dict[str, Any]]:
    """The system prompt as blocks, in the order the cache needs: the stable text with the
    breakpoint on it, then the per-run text after it. Tools come before the system prompt in
    the cached prefix, so one breakpoint here covers both."""
    blocks: list[dict[str, Any]] = [
        {"type": "text", "text": request.system, "cache_control": dict(CACHE_CONTROL)}
    ]
    if request.system_run:
        blocks.append({"type": "text", "text": request.system_run})
    return blocks


def cached_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copies of the tool definitions with a breakpoint on the last one, which caches all of
    them. Copies, because the originals are shared (`tool_specs` caches them)."""
    marked = [dict(t) for t in tools]
    marked[-1]["cache_control"] = dict(CACHE_CONTROL)
    return marked


def _to_response(raw: Any) -> LLMResponse:
    text_parts: list[str] = []
    tool_uses: list[ToolUse] = []
    content: list[dict[str, Any]] = []
    for block in raw.content:
        content.append(block.model_dump(mode="json"))
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            tool_uses.append(ToolUse(id=block.id, name=block.name, input=dict(block.input)))
    return LLMResponse(
        content=content,
        text="".join(text_parts),
        tool_uses=tool_uses,
        stop_reason=_STOP_REASONS.get(raw.stop_reason, "other"),
        stop_details=_stop_details(raw),
        usage=Usage(
            input_tokens=raw.usage.input_tokens,
            output_tokens=raw.usage.output_tokens,
            # None, or absent, when the API reports no cache activity.
            cache_creation_input_tokens=getattr(raw.usage, "cache_creation_input_tokens", 0) or 0,
            cache_read_input_tokens=getattr(raw.usage, "cache_read_input_tokens", 0) or 0,
        ),
    )


def _stop_details(raw: Any) -> dict[str, Any] | None:
    details = getattr(raw, "stop_details", None)
    if details is None:
        return None
    return details.model_dump(mode="json") if hasattr(details, "model_dump") else dict(details)


def model_name(provider: Any) -> str:
    """The model a provider calls. The fixture provider replays runs recorded on MODEL."""
    return str(getattr(provider, "model", None) or MODEL)


def estimate_cost_usd(usage: Usage, model: str = MODEL) -> float | None:
    """Estimated from `model`'s list price (PRICES), or None when the model is not listed or a
    request's prompt was long enough for a rate card the table does not carry: "unknown" rather
    than another model's number."""
    price = PRICES.get(model)
    if price is None:
        return None
    if price.long_prompt_tokens is not None and usage.peak_prompt_tokens > price.long_prompt_tokens:
        return None
    in_tokens = usage.input_tokens + usage.cache_creation_input_tokens * CACHE_WRITE_PRICE_FACTOR
    return round(
        in_tokens * price.in_per_mtok / 1e6
        + usage.cache_read_input_tokens * price.cache_read_per_mtok / 1e6
        + usage.output_tokens * price.out_per_mtok / 1e6,
        4,
    )


def _live_api_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise LLMConfigError(
            "LLM_MODE=live needs ANTHROPIC_API_KEY: export it in the process environment "
            "(backend/.env is not read for it yet)"
        )
    return key


def get_provider(settings: Settings) -> LLMProvider:
    """Fixture mode never reads the key. Live mode fails early if it is missing. The model
    (LLM_MODEL) and the API address (ANTHROPIC_BASE_URL) come from the settings; empty means
    the built-in default model and Anthropic's own API."""
    if settings.llm_mode == "fixture":
        return FixtureProvider()
    return AnthropicProvider(
        api_key=_live_api_key(), model=settings.llm_model or MODEL,
        base_url=settings.anthropic_base_url or None,
    )
