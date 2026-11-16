"""The step runner (BUILD_PLAN section 5).

`execute_run` walks the run's steps in order and saves the run row after every step, so the
caller (the worker, later) can resume after a crash from the first unfinished step. One nonce
names the evidence tag for the whole run (saved on the steps, so a resume reuses it), and one
deadline (`RUN_BUDGET_S`) wraps the whole run, not each model call.

Crash safety: the loop commits once per turn, and the runner writes the step (tool calls, usage
and a checkpoint of the conversation) in that same commit. A killed worker loses at most the
open turn together with its rows, and `execute_run` on that run continues from the checkpoint.

The `extract` and `timeline` steps exist so far. `extract` runs first, at the start of a run,
so it needs nothing from ingest: it reads the `evidence_page` rows that ingest wrote. `ingest`
needs WS1, `rules` and `gaps` need WS4, and `draft` is deterministic and comes next; those steps
stay `pending` here.

Pause and resume: when the agent calls ask_caseworker the step becomes `needs_input` and the
conversation is saved in the step's `checkpoint` key (the API never returns it). Call
`execute_run(..., answer=...)` on that run to resume.

The hearing date is asked by code, not left to the model: when the timeline step ends and the
case has no hearing date and the model never asked anything, the runner pauses the step with
the same ask_caseworker pause (field `hearing_date`). That pause is `synthetic`: there is no
model turn to continue, so the answer just completes the step."""

import asyncio
import copy
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from app.models import CaseRow, RunRow
from app.schemas import ToolCall
from app.services import activity
from app.services.agent import extract, prompts
from app.services.agent.evidence_text import EvidenceTextStore
from app.services.agent.llm import (
    RUN_BUDGET_S,
    LLMProvider,
    Message,
    Usage,
    estimate_cost_usd,
    model_name,
)
from app.services.agent.loop import (
    LoopResult,
    StepState,
    TurnHook,
    answer_block,
    run_agent_loop,
)
from app.services.agent.tools import AGENT_ACTOR, ProposeEventOutput, ToolContext, new_nonce
from app.services.mappers import case_counts
from app.services.stage import refresh_stage

STEP_LABELS = {
    "ingest": "Check files",
    "extract": "Extract facts",
    "timeline": "Build timeline",
    "rules": "Look up official rules",
    "gaps": "Find missing information",
    "draft": "Draft form",
}
IMPLEMENTED_STEPS = ("extract", "timeline")
HEARING_QUESTION = "What is the hearing date? It is not in the evidence."
log = logging.getLogger(__name__)


class RunNotWaiting(Exception):
    """A resume for a run that is not paused: already resumed, or never paused."""


def create_run(session: Session, case: CaseRow) -> RunRow:
    """A new run with the six contract steps, all pending."""
    run = RunRow(
        id=f"run_{uuid.uuid4().hex[:10]}",
        case_id=case.id,
        status="running",
        started_at=datetime.now(UTC),
        steps=[
            {"key": key, "label": label, "status": "pending", "summary": None, "tool_calls": []}
            for key, label in STEP_LABELS.items()
        ],
    )
    session.add(run)
    activity.log(session, case.id, "agent", AGENT_ACTOR, f"Started {run.id}")
    session.commit()
    return run


# --------------------------------------------------------------------------- step state


def _load_state(step: dict[str, Any], answer: str | None) -> StepState:
    """A fresh state, or the saved one: after a pause (needs the answer) or after a crash."""
    checkpoint = step.get("checkpoint")
    if checkpoint is None:
        return StepState(messages=[Message(role="user", content=prompts.TIMELINE_KICKOFF)])
    messages = [Message.model_validate(m) for m in checkpoint["messages"]]
    pending = checkpoint.get("pending")
    if pending is not None:
        if answer is None:
            raise ValueError("this step is waiting for the caseworker's answer")
        blocks = [*pending["results"], answer_block(pending["tool_use_id"], answer)]
        messages.append(Message(role="user", content=blocks))
    return StepState(
        messages=messages,
        tool_calls=[ToolCall.model_validate(c) for c in step["tool_calls"]],
        usage=Usage.model_validate(checkpoint["usage"]),
        turns=checkpoint["turns"],
    )


def _write_step(
    step: dict[str, Any],
    state: StepState,
    status: str,
    summary: str | None,
    pending: dict[str, Any] | None = None,
) -> None:
    step["status"] = status
    step["summary"] = summary
    step["tool_calls"] = [c.model_dump() for c in state.tool_calls]
    step["usage"] = state.usage.model_dump()
    step.pop("checkpoint", None)
    if status in ("running", "needs_input"):  # resumable: a crash and a pause both resume here
        step["checkpoint"] = {
            "messages": [m.model_dump(mode="json") for m in state.messages],
            "pending": pending,
            "usage": state.usage.model_dump(),
            "turns": state.turns,
        }


def _tally(state: StepState) -> str:
    if any(c.tool == extract.STEP_TOOL for c in state.tool_calls):
        outs = [json.loads(c.output) for c in state.tool_calls if c.tool == extract.STEP_TOOL]
        saved = sum(o.get("saved", 0) for o in outs)
        dropped = sum(o.get("dropped", 0) for o in outs)
        return f"{saved} facts saved, {dropped} dropped"
    accepted = rejected = 0
    for call in state.tool_calls:
        if call.tool != "propose_event":
            continue
        try:
            out = ProposeEventOutput.model_validate_json(call.output)
        except ValueError:
            continue  # the call was a ToolError, not a proposal
        accepted += out.accepted
        rejected += len(out.rejected_reasons)
    return f"{accepted} events proposed, {rejected} citations rejected"


def _write_extract(
    step: dict[str, Any], state: StepState, status: str, summary: str | None
) -> None:
    """The extract step has no conversation to checkpoint: each item is committed with its
    facts, and a resume runs the step again (finished items are cache hits)."""
    step["status"] = status
    step["summary"] = summary
    step["tool_calls"] = [c.model_dump() for c in state.tool_calls]
    step["usage"] = state.usage.model_dump()
    step.pop("checkpoint", None)


_STEP_STATUS = {
    "finished": "done", "budget": "done", "needs_input": "needs_input",
    "refused": "failed", "llm_error": "failed",
}  # fmt: skip


def _apply_result(step: dict[str, Any], state: StepState, result: LoopResult) -> None:
    status = _STEP_STATUS[result.status]
    summary = result.summary
    if status != "needs_input":
        summary = f"{summary} ({_tally(state)})"
    _write_step(step, state, status, summary, result.pending)


# --------------------------------------------------------------------------- the run


def _usage_totals(run: RunRow, duration_ms: int, model: str) -> dict[str, Any]:
    total = Usage()
    for step in run.steps:
        if step.get("usage"):
            total += Usage.model_validate(step["usage"])
    return {
        "input_tokens": total.input_tokens,
        "output_tokens": total.output_tokens,
        # The cache counts are stored on the run, not part of the API's RunUsage.
        "cache_creation_input_tokens": total.cache_creation_input_tokens,
        "cache_read_input_tokens": total.cache_read_input_tokens,
        "model": model,
        "est_cost_usd": estimate_cost_usd(total, model),  # None: no price listed for this model
        "duration_ms": duration_ms,
    }


def _final_status(session: Session, run: RunRow) -> str:
    """CONTRACT_CHANGES #4: failed if a step failed; needs_review if a step needs input, an
    event is pending or a blocking gap is open; else done."""
    statuses = {s["status"] for s in run.steps}
    if "failed" in statuses:
        return "failed"
    blocking, pending = case_counts(session, run.case_id)
    if "needs_input" in statuses or blocking or pending:
        return "needs_review"
    return "done"


def paused_question(run: RunRow) -> dict[str, Any] | None:
    """The saved pause of a run waiting for the caseworker: the ask_caseworker tool_use id, the
    other calls' results and the question's field key (or None). None when nothing the runner
    can resume is waiting, for example a seeded run whose gaps step says `needs_input`."""
    if run.status != "needs_review":
        return None
    for step in run.steps:
        if step["key"] in IMPLEMENTED_STEPS and step["status"] == "needs_input":
            pending = (step.get("checkpoint") or {}).get("pending")
            if pending:
                return pending
    return None


def claim_pause(session: Session, run: RunRow) -> None:
    """Move a paused run to running, once. Of two answers for one pause only one gets through;
    the other raises RunNotWaiting. Rolls back anything the caller staged in the session, and
    commits it together with the claim when it succeeds. Pass `claimed=True` to execute_run."""
    _claim(session, run, IMPLEMENTED_STEPS)


def _claim(session: Session, run: RunRow, steps: tuple[str, ...]) -> None:
    """Move a paused run to running. The row is locked and re-read first, so of two answers sent
    for the same pause only one resumes it; the other finds the step no longer waiting."""
    stmt = select(RunRow).where(RunRow.id == run.id).with_for_update()
    row = session.exec(stmt.execution_options(populate_existing=True)).one()
    still_waiting = row.status == "needs_review" and any(
        s["status"] == "needs_input" for s in row.steps if s["key"] in steps
    )
    if not still_waiting:
        session.rollback()
        raise RunNotWaiting(run.id)
    row.status = "running"
    session.add(row)
    session.commit()


async def _run_timeline(
    ctx: ToolContext, provider: LLMProvider, state: StepState, on_turn: TurnHook
) -> LoopResult:
    return await run_agent_loop(
        provider, ctx, state, step_key="timeline", system=prompts.timeline_system(),
        system_run=prompts.timeline_run_note(ctx.nonce),
        tool_names=prompts.TIMELINE_TOOLS, on_turn=on_turn,
    )  # fmt: skip


def _step(all_steps: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return next(s for s in all_steps if s["key"] == key)


def _needs_hearing_ask(case: CaseRow, state: StepState, result: LoopResult) -> bool:
    """Ask for the hearing date when the step ended normally, the case has none, and the model
    did not ask anything itself (one question per step, not two)."""
    if result.status not in ("finished", "budget") or case.hearing_date is not None:
        return False
    return not any(c.tool == "ask_caseworker" for c in state.tool_calls)


def _hearing_pause(step: dict[str, Any], state: StepState) -> LoopResult:
    """Pause the finished step with the ask_caseworker pause. The question is logged like a
    model's call; the step's own summary is kept in the pause and restored on the answer."""
    state.tool_calls.append(ToolCall(
        tool="ask_caseworker",
        input=json.dumps({"question": HEARING_QUESTION, "field": "hearing_date"}),
        output='{"paused":true}'))
    pending = {
        "tool_use_id": "", "results": [], "field": "hearing_date", "synthetic": True,
        "summary": step["summary"],
    }
    _write_step(step, state, "needs_input", HEARING_QUESTION, pending)
    return LoopResult("needs_input", HEARING_QUESTION, pending=pending)


async def execute_run(
    session: Session,
    run: RunRow,
    provider: LLMProvider,
    texts: EvidenceTextStore,
    *,
    nonce: str | None = None,
    deadline_s: float = RUN_BUDGET_S,
    answer: str | None = None,
    steps: tuple[str, ...] = IMPLEMENTED_STEPS,
    claimed: bool = False,
) -> RunRow:
    """Run the given steps that are not done yet. The deadline covers all of them together; on a
    resume it is what is left of the budget after the time earlier calls already used. The
    nonce is chosen once and kept on the steps: a resume reuses it, and passing a different one
    is an error."""
    case = session.get(CaseRow, run.case_id)
    if case is None:
        raise ValueError("run has no case")
    unknown = [k for k in steps if k not in IMPLEMENTED_STEPS]
    if unknown:
        raise ValueError(f"steps not implemented yet: {', '.join(unknown)}")
    all_steps = copy.deepcopy(run.steps)
    waiting = any(_step(all_steps, k)["status"] == "needs_input" for k in steps)
    if waiting and answer is None:
        raise ValueError("this run is waiting for the caseworker's answer")
    stored = next((s["nonce"] for s in all_steps if s.get("nonce")), None)
    if nonce is not None and stored is not None and nonce != stored:
        raise ValueError("this run already uses a different evidence nonce")
    if waiting:
        if not claimed:  # the answer route already claimed the pause (claim_pause)
            _claim(session, run, steps)
        all_steps = copy.deepcopy(run.steps)  # re-read under the lock
    ctx = ToolContext(
        session=session, org_id=case.org_id, case_id=case.id, run_id=run.id, texts=texts,
        nonce=stored or nonce or new_nonce(),
    )  # fmt: skip
    previous_ms = int((run.usage or {}).get("duration_ms", 0))
    remaining = max(deadline_s - previous_ms / 1000, 0.001)
    started = time.monotonic()
    run.status = "running"
    run.finished_at = None
    session.add(run)

    current: tuple[dict[str, Any], StepState] | None = None
    try:
        async with asyncio.timeout(remaining):
            for key in steps:
                step = _step(all_steps, key)
                if step["status"] == "done":
                    continue  # a resume skips finished steps
                if key == "extract":
                    state = StepState(messages=[])
                    current = (step, state)
                    step["nonce"] = ctx.nonce
                    _write_extract(step, state, "running", None)
                    run.steps = copy.deepcopy(all_steps)
                    session.commit()

                    def on_item(item: extract.ItemResult, step=step, state=state) -> None:
                        state.tool_calls.append(ToolCall(
                            tool=extract.STEP_TOOL, input=item.tool_call_input(),
                            output=item.tool_call_output()))
                        state.usage = state.usage + item.usage
                        _write_extract(step, state, "running", None)
                        run.steps = copy.deepcopy(all_steps)
                        session.commit()  # this item's facts and the step, together

                    done = await extract.extract_case(
                        session, case.id, case.org_id, texts, provider, ctx.nonce, on_item)
                    _write_extract(step, state, "done", done.text)
                    current = None
                    run.steps = copy.deepcopy(all_steps)
                    session.commit()
                    continue
                saved = (step.get("checkpoint") or {}).get("pending") or {}
                state = _load_state(step, answer)
                current = (step, state)
                step["nonce"] = ctx.nonce
                if saved.get("synthetic"):  # the hearing-date answer: the step is complete
                    _write_step(step, state, "done", saved["summary"])
                    current = None
                    run.steps = copy.deepcopy(all_steps)
                    session.commit()
                    continue
                step["status"] = "running"
                run.steps = copy.deepcopy(all_steps)
                session.commit()

                def on_turn(st: StepState, pending: dict[str, Any] | None, step=step) -> None:
                    _write_step(step, st, "running", None, pending)
                    run.steps = copy.deepcopy(all_steps)

                result = await _run_timeline(ctx, provider, state, on_turn)
                _apply_result(step, state, result)
                if _needs_hearing_ask(case, state, result):
                    result = _hearing_pause(step, state)
                current = None
                run.steps = copy.deepcopy(all_steps)
                session.commit()
                if result.status == "needs_input":
                    break  # paused: nothing after this step runs until the answer comes
    except TimeoutError:
        session.rollback()  # only a model call can be cut off; no turn is open
        if current is not None:
            step, state = current
            why = f"Stopped early: the {deadline_s:.0f} s run deadline passed ({_tally(state)})."
            _write_step(step, state, "done", why)
    except asyncio.CancelledError:
        session.rollback()  # the step stays running with its last checkpoint: it can resume
        raise
    except Exception as exc:  # a failed step must not leave the run "running" forever
        session.rollback()
        log.warning("run %s step failed: %s", run.id, type(exc).__name__)  # type only
        if current is not None:
            step, state = current
            _write_step(step, state, "failed", f"The step failed ({type(exc).__name__}).")
    run.steps = copy.deepcopy(all_steps)
    run.usage = _usage_totals(
        run, previous_ms + int((time.monotonic() - started) * 1000), model_name(provider))
    run.status = _final_status(session, run)
    paused = any(s["status"] == "needs_input" for s in run.steps)
    run.finished_at = None if paused else datetime.now(UTC)
    activity.log(session, case.id, "agent", AGENT_ACTOR, _activity_line(run, paused))
    refresh_stage(session, case)
    session.add(run)
    session.commit()
    return run


def _activity_line(run: RunRow, paused: bool) -> str:
    if paused:
        return f"Paused {run.id}: waiting for the caseworker's answer"
    done = next((s for s in run.steps if s["key"] == "timeline"), None)
    detail = f": {done['summary']}" if done and done.get("summary") else ""
    return f"Finished {run.id} ({run.status}){detail}"
