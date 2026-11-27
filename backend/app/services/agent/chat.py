"""OpenAI-compatible transport for the existing case-scoped tool loop."""

import asyncio
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field, NonNegativeInt, ValidationError

from app.services.agent.llm import (
    CALL_TIMEOUT_S,
    MAX_RETRIES,
    LLMCallError,
    LLMRequest,
    LLMResponse,
    LLMTimeout,
    Message,
    ToolUse,
    Usage,
)

# Relay only fields used by the supported tool/continuation protocols.
# Chat Completions endpoints often cap output tokens well below the 16000 the loop asks for
# (DeepSeek's chat models allow 8192). A value over the provider's limit fails the whole call
# with a 400, so chat adapters clamp to a value every supported provider accepts.
CHAT_MAX_OUTPUT_TOKENS = 8000

_MESSAGE_EXTRAS = {
    "deepseek": ("reasoning_content",),
    "openrouter": ("reasoning_details",),
    "gemini": ("extra_content",),
    "openai_compatible": ("reasoning_content", "reasoning_details", "extra_content"),
}


class _Function(BaseModel):
    name: str = Field(min_length=1)
    arguments: str


class _ToolCall(BaseModel):
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: _Function
    extra_content: dict[str, Any] | None = None


class _Assistant(BaseModel):
    content: str | None = None
    refusal: str | None = None
    tool_calls: list[_ToolCall] | None = None


class _PromptTokens(BaseModel):
    cached_tokens: NonNegativeInt = 0
    cache_write_tokens: NonNegativeInt = 0


class _Usage(BaseModel):
    prompt_tokens: NonNegativeInt = 0
    completion_tokens: NonNegativeInt = 0
    total_tokens: NonNegativeInt = 0
    prompt_tokens_details: _PromptTokens | None = None
    prompt_cache_hit_tokens: NonNegativeInt = 0

    def for_loop(self) -> Usage:
        read = (self.prompt_tokens_details.cached_tokens if self.prompt_tokens_details else
                self.prompt_cache_hit_tokens)
        write = self.prompt_tokens_details.cache_write_tokens if self.prompt_tokens_details else 0
        if read + write > self.prompt_tokens:
            raise LLMCallError("Provider returned inconsistent token usage")
        return Usage(
            input_tokens=self.prompt_tokens - read - write,
            output_tokens=max(self.completion_tokens, self.total_tokens - self.prompt_tokens),
            cache_read_input_tokens=read,
            cache_creation_input_tokens=write,
        )


def _conversation_message(message: Message) -> list[dict[str, Any]]:
    if isinstance(message.content, str):
        return [{"role": message.role, "content": message.content}]
    if message.role == "assistant":
        blocks = message.content
        if len(blocks) == 1 and blocks[0].get("type") == "chat_assistant":
            return [blocks[0]["message"]]
        raise LLMCallError("The assistant history uses a different model API")
    messages: list[dict[str, Any]] = []
    for block in message.content:
        if block.get("type") == "tool_result":
            messages.append({
                "role": "tool", "tool_call_id": block["tool_use_id"], "content": block["content"],
            })
        elif block.get("type") == "text":
            messages.append({"role": "user", "content": block["text"]})
        else:
            raise LLMCallError("Unsupported conversation content for this model API")
    return messages


def _result(raw: Any, provider: str) -> LLMResponse:
    try:
        choice = raw["choices"][0]
        message = choice["message"]
        finish = choice["finish_reason"]
        usage = _Usage.model_validate(raw.get("usage") or {}).for_loop()
        # Filtered and interrupted responses cannot dispatch tools, even with partial tool data.
        if finish == "content_filter" or message.get("refusal"):
            return LLMResponse(stop_reason="refusal", usage=usage)
        if finish == "length":
            return LLMResponse(stop_reason="max_tokens", usage=usage)
        assistant = _Assistant.model_validate(message)
        calls = assistant.tool_calls or []
        # Some OpenAI-compatible servers (Gemini's endpoint was reported) send "stop" for a
        # function-call turn. Tool use is decided by the calls themselves; "tool_calls" without
        # calls is still malformed. "length" and "content_filter" are refused above.
        if finish not in ("stop", "tool_calls") or (finish == "tool_calls" and not calls):
            raise LLMCallError("Provider returned an unsupported completion state")
        if len({call.id for call in calls}) != len(calls):
            raise LLMCallError("Provider returned duplicate tool call identifiers")
        uses = []
        for call in calls:
            arguments = json.loads(call.function.arguments)
            if not isinstance(arguments, dict):
                raise LLMCallError("Provider returned invalid tool arguments")
            uses.append(ToolUse(id=call.id, name=call.function.name, input=arguments))
        history: dict[str, Any] = {
            "role": "assistant",
            "content": assistant.content,
            **{key: message[key] for key in _MESSAGE_EXTRAS.get(provider, ()) if key in message},
        }
        if calls:
            # Preserve extension fields such as Gemini thought signatures for the next call.
            history["tool_calls"] = [call.model_dump(exclude_none=True) for call in calls]
        return LLMResponse(
            text=assistant.content or "",
            tool_uses=uses,
            content=[{"type": "chat_assistant", "message": history}],
            usage=usage,
            stop_reason="tool_use" if calls else "end_turn",
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValidationError, ValueError):
        raise LLMCallError("Provider returned an invalid chat completion") from None


def _status_error(response: httpx.Response, secret: str = "") -> str:
    """The HTTP status plus the provider's own message, never the request, headers or key."""
    if response.status_code in (401, 403):
        return f"Provider HTTP status {response.status_code}"
    detail = ""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
        if not detail:
            detail = str(data.get("message") or "")
    # Only a structured provider message is repeated: a plain body may echo the request or the
    # key (the 307 body in the tests is the key itself), so it is never surfaced.
    if secret:
        detail = detail.replace(secret, "[redacted]")
    detail = " ".join(detail.split())[:200]
    return f"Provider HTTP status {response.status_code}" + (f": {detail}" if detail else "")


class ChatCompletionsProvider:
    """Adapter for Chat Completions endpoints with locally dispatched function tools."""

    requires_continuation_key = True

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        provider: str = "openai",
        base_url: str = "https://api.openai.com/v1",
        effort: str = "",
        deadline_s: float = CALL_TIMEOUT_S,
        max_output_tokens: int = CHAT_MAX_OUTPUT_TOKENS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.effort = effort
        self.deadline_s = deadline_s
        self.max_output_tokens = max_output_tokens
        self.transport = transport
        self.continuation_profile = (provider, model, self.base_url, effort)

    def _payload(self, request: LLMRequest) -> dict[str, Any]:
        system = "\n\n".join(text for text in (request.system, request.system_run) if text)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                *(item for message in request.messages for item in _conversation_message(message)),
            ],
        }
        token_key = "max_completion_tokens" if self.provider in {"openai", "groq"} else "max_tokens"
        payload[token_key] = min(request.max_tokens, self.max_output_tokens)
        if request.tools:
            payload["tools"] = [{
                "type": "function",
                "function": {
                    "name": spec["name"], "description": spec["description"],
                    "parameters": spec["input_schema"],
                },
            } for spec in request.tools]
        if self.provider == "openrouter":
            payload["provider"] = {"allow_fallbacks": False, "require_parameters": True}
        if self.effort:
            if self.provider == "openrouter":
                payload["reasoning"] = {"effort": self.effort}
            else:
                payload["reasoning_effort"] = self.effort
            if self.provider == "deepseek":
                payload["thinking"] = {"type": "enabled"}
        return payload

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload = self._payload(request)
        deadline = asyncio.get_running_loop().time() + self.deadline_s
        try:
            async with asyncio.timeout_at(deadline):
                async with httpx.AsyncClient(
                    transport=self.transport, timeout=self.deadline_s, follow_redirects=False,
                ) as client:
                    response = await self._post(client, payload, deadline)
                return _result(response.json(), self.provider)
        except (TimeoutError, httpx.TimeoutException):
            raise LLMTimeout("provider request timed out") from None
        except httpx.HTTPStatusError as exc:
            raise LLMCallError(_status_error(exc.response, self._api_key)) from None
        except httpx.HTTPError as exc:
            raise LLMCallError(type(exc).__name__) from None
        except ValueError:
            raise LLMCallError("Provider returned an invalid JSON response") from None


    async def _post(
        self, client: httpx.AsyncClient, payload: dict[str, Any], deadline: float
    ) -> httpx.Response:
        loop = asyncio.get_running_loop()
        for attempt in range(MAX_RETRIES):
            delay = 0.5 * 2**attempt
            try:
                return await self._post_once(client, payload)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if delay >= deadline - loop.time():
                    raise
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if code != 429 and not 500 <= code <= 599:
                    raise
                delay = _retry_delay(exc.response, delay)
                if delay >= deadline - loop.time():
                    raise
            await asyncio.sleep(delay)
        return await self._post_once(client, payload)

    async def _post_once(
        self, client: httpx.AsyncClient, payload: dict[str, Any]
    ) -> httpx.Response:
        response = await client.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
        )
        return response.raise_for_status()


def _retry_delay(response: httpx.Response, fallback: float) -> float:
    value = response.headers.get("Retry-After", "").strip()
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
