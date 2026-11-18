"""The bounded agent loop (BUILD_PLAN section 5).

One call to `run_agent_loop` is one step: the model asks for tools, the tools run against the
database, the results go back, until the model finishes, asks the caseworker, or a budget runs
out. The assistant turn is appended exactly as the API returned it (`LLMResponse.content`, thinking
blocks included). The wall-clock deadline is not here: the runner puts one around the whole run.

`StepState` is mutated as the loop goes, so when the runner's deadline cancels the loop the tool
calls made so far are still in it."""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.schemas import ToolCall
from app.services.agent.llm import LLMError, LLMProvider, LLMRequest, Message, ToolUse, Usage
from app.services.agent.tools import (
    TOOLS,
    ToolContext,
    ToolError,
    clean_text,
    to_tool_call,
    tool_specs,
    validate_input,
)

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 25
MAX_TOTAL_TOKENS = 200_000  # input plus output, summed over the turns of one step
MAX_TURNS = 40  # model calls, so a model that never calls a tool still stops
SUMMARY_CHARS = 1000

# Called at the end of every turn that ran tools, just before the commit: (state, pending). The
# runner saves the step (tool calls, usage, a checkpoint) there, so the turn's rows and the
# checkpoint are one transaction. A crash loses at most the open turn, and its rows with it.
TurnHook = Callable[["StepState", dict[str, Any] | None], None]

LoopStatus = Literal["finished", "needs_input", "budget", "refused", "llm_error"]


@dataclass(frozen=True)
class LoopBudget:
    max_tool_calls: int = MAX_TOOL_CALLS
    max_tokens: int = MAX_TOTAL_TOKENS
    max_turns: int = MAX_TURNS


@dataclass
class StepState:
    """Everything a step needs to be saved and resumed."""

    messages: list[Message]
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    turns: int = 0


@dataclass(frozen=True)
class LoopResult:
    status: LoopStatus
    summary: str  # for needs_input: the question
    # needs_input only: the ask_caseworker tool_use id, and the tool_result blocks of the other
    # tool calls in the same assistant turn. Both go back to the model on resume.
    pending: dict[str, Any] | None = None


def _short(text: str) -> str:
    return clean_text(text, SUMMARY_CHARS)


async def _dispatch(ctx: ToolContext, use: ToolUse, allowed: set[str]) -> BaseModel:
    tool = TOOLS.get(use.name) if use.name in allowed else None
    if tool is None:
        return ToolError(
            tool=use.name[:64], kind="invalid_input",
            message="Unknown tool. Use one you were given.",
        )  # fmt: skip
    parsed = validate_input(tool, use.input)
    if isinstance(parsed, ToolError):
        return parsed
    try:
        return await tool.run(ctx, parsed)
    except Exception as exc:  # a tool bug must not end the run; the model gets a plain error
        ctx.session.rollback()
        log.warning("tool %s failed: %s", use.name, type(exc).__name__)  # type only: no row data
        return ToolError(
            tool=use.name, kind="unavailable", message="The tool failed. Try another approach."
        )


def _result_block(use: ToolUse, result: BaseModel) -> dict[str, Any]:
    return {
        "type": "tool_result",
        "tool_use_id": use.id,
        "content": result.model_dump_json(),
        "is_error": isinstance(result, ToolError),
    }


def _error_block(use: ToolUse, message: str) -> dict[str, Any]:
    """A tool_result for a call that was not run. Every tool_use gets exactly one result."""
    payload = json.dumps({"ok": False, "message": message})
    return {"type": "tool_result", "tool_use_id": use.id, "content": payload, "is_error": True}


def _end_turn(
    ctx: ToolContext, state: StepState, on_turn: TurnHook | None, pending: dict[str, Any] | None
) -> None:
    if on_turn is not None:
        on_turn(state, pending)
    ctx.session.commit()


def answer_block(tool_use_id: str, answer: str) -> dict[str, Any]:
    """The tool_result that answers a paused ask_caseworker call."""
    payload = json.dumps({"ok": True, "answer": _short(answer)}, ensure_ascii=False)
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": payload}


def _over_budget(state: StepState, budget: LoopBudget) -> str | None:
    if len(state.tool_calls) >= budget.max_tool_calls:
        return f"{budget.max_tool_calls} tool calls"
    if state.usage.input_tokens + state.usage.output_tokens >= budget.max_tokens:
        return f"{budget.max_tokens} tokens"
    if state.turns >= budget.max_turns:
        return f"{budget.max_turns} model calls"
    return None


async def run_agent_loop(
    provider: LLMProvider,
    ctx: ToolContext,
    state: StepState,
    *,
    step_key: str,
    system: str,
    system_run: str = "",
    tool_names: list[str],
    budget: LoopBudget | None = None,
    on_turn: TurnHook | None = None,
) -> LoopResult:
    budget = budget or LoopBudget()
    specs = tool_specs(tool_names)
    allowed = set(tool_names)
    while True:
        spent = _over_budget(state, budget)
        if spent:
            return LoopResult("budget", f"Stopped early: the step reached its limit of {spent}.")
        state.turns += 1
        request = LLMRequest(
            system=system, system_run=system_run, messages=list(state.messages), tools=specs,
            fixture_key=f"{step_key}-turn{state.turns}",
        )  # fmt: skip
        try:
            response = await provider.complete(request)
        except LLMError as exc:
            return LoopResult("llm_error", _short(f"The model call failed: {exc}"))
        state.usage += response.usage
        state.messages.append(response.assistant_message())  # unchanged, thinking included

        if response.stop_reason == "refusal":
            category = (response.stop_details or {}).get("category")
            why = f" ({category})" if category else ""
            return LoopResult("refused", _short(f"The model declined this step{why}."))
        if response.stop_reason == "max_tokens":
            return LoopResult("budget", "Stopped early: the model reached its output limit.")
        if response.stop_reason == "pause_turn":
            continue  # the API asked us to send the turn back as it is
        if not response.tool_uses:
            return LoopResult("finished", _short(response.text) or "Ended without calling finish.")

        results: list[dict[str, Any]] = []
        asked: tuple[ToolUse, str] | None = None
        finished: str | None = None
        cut = False
        for use in response.tool_uses:
            if use.name == "ask_caseworker" and asked is not None:
                results.append(_error_block(use, "Only one question per turn: one is pending."))
                continue
            if len(state.tool_calls) >= budget.max_tool_calls:
                results.append(_error_block(use, "Skipped: the step reached its tool-call limit."))
                cut = True
                continue
            result = await _dispatch(ctx, use, allowed)
            state.tool_calls.append(to_tool_call(use.name, use.input, result))
            failed = isinstance(result, ToolError)
            if use.name == "ask_caseworker" and not failed:
                asked = (use, _short(str(use.input.get("question", ""))))
                continue  # its tool_result is the caseworker's answer, later
            results.append(_result_block(use, result))
            if use.name == "finish" and not failed:
                finished = _short(str(use.input.get("summary", "")))
        if asked is not None:
            use, question = asked
            pending = {
                "tool_use_id": use.id, "results": results, "field": use.input.get("field"),
            }
            _end_turn(ctx, state, on_turn, pending)
            return LoopResult("needs_input", question, pending=pending)
        if finished is not None or cut:
            _end_turn(ctx, state, on_turn, None)
            if finished is not None:
                return LoopResult("finished", finished)
            return LoopResult(
                "budget",
                f"Stopped early: the step reached its limit of {budget.max_tool_calls} tool calls.",
            )
        state.messages.append(Message(role="user", content=results))
        _end_turn(ctx, state, on_turn, None)
