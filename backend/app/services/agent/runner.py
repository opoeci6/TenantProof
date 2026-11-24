"""The step runner (BUILD_PLAN section 5).

`execute_run` walks the run's steps in order and saves the run row after every step, so the
caller (the worker, later) can resume after a crash from the first unfinished step. One nonce
names the evidence tag for the whole run (saved on the steps, so a resume reuses it), and one
deadline (the `RUN_BUDGET_S` setting, 180 s by default) wraps the whole run, not each model
call.

Crash safety: the loop commits once per turn, and the runner writes the step (tool calls, usage
and a checkpoint of the conversation) in that same commit. A killed worker loses at most the
open turn together with its rows, and `execute_run` on that run continues from the checkpoint.

All steps but `ingest` exist, in this order: `extract`, `timeline`, `rules`, `draft`, `gaps`.
`extract` runs first, at the start of a run, so it needs nothing from ingest: it reads the
`evidence_page` rows that ingest wrote. `rules` is deterministic (WS4): it gives the case the
corpus sources and the evidence deadline, and it runs after `timeline` because the hearing date it
needs comes from there. `draft` (#40) is an agent loop that fills the form fields still `missing`
from the evidence, each value copied from a citation the verifier accepts and saved as
`needs_confirmation`. BUILD_PLAN planned it deterministic, but the extracted facts carry no field
key, so per-field patterns would not hold across documents and languages. With nothing to fill it
makes no model call. It runs before `gaps`, so the gaps step only asks the tenant for what the
evidence does not say. `gaps` (WS4) writes the deterministic gaps first, then runs an agent loop
that reads the evidence and drafts requests to the tenant. `ingest` needs WS1 and stays `pending`.

Pause and resume: when the agent calls ask_caseworker the step becomes `needs_input` and the
conversation is saved in the step's `checkpoint` key (the API never returns it). Call
`execute_run(..., answer=...)` on that run to resume.

The hearing date is asked by code, not left to the model: when the timeline step ends and the
case has no hearing date and the model never asked anything, the runner pauses the step with
the same ask_caseworker pause (field `hearing_date`). That pause is `synthetic`: there is no
model turn to continue, so the answer just completes the step.

A step cut off by a limit or the run deadline ends `incomplete` (#119), not `done`. It is over all
the same: a resume does not run it again, and a hearing-date answer leaves it `incomplete`."""

import asyncio
import copy
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, col, select

from app.config import get_settings
from app.models import CaseRow, EvidenceRow, FormFieldRow, RunRow, TimelineEventRow
from app.schemas import ToolCall
from app.services import activity
from app.services.agent import extract, prompts
from app.services.agent.evidence_text import EvidenceTextStore
from app.services.agent.llm import (
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
from app.services.agent.tools import (
    AGENT_ACTOR,
    CreateGapOutput,
    DraftTenantMessageOutput,
    FillFormFieldOutput,
    ProposeEventOutput,
    ToolContext,
    clean_text,
    is_fillable,
    new_nonce,
)
from app.services.form_fields import covered_legacy_fields
from app.services.legal import gaps
from app.services.legal import rules as legal_rules
from app.services.mappers import case_counts
from app.services.stage import refresh_stage

STEP_LABELS = {
    "ingest": "Check files",
    "extract": "Extract facts",
    "timeline": "Build timeline",
    "rules": "Look up official rules",
    # `draft` runs before `gaps` (#40): once the form agent has filled what the evidence states,
    # the gaps step no longer asks the tenant for her own name or the landlord's address.
    "draft": "Draft form",
    "gaps": "Find missing information",
}
IMPLEMENTED_STEPS = ("extract", "timeline", "rules", "draft", "gaps")
HEARING_QUESTION = "What is the hearing date? It is not in the evidence."
OLD_EVENTS_KEPT = "The events from the earlier analysis were kept."
RUN_CLOSED = "Closed: a newer analysis replaced this run."
STEP_OVER = ("done", "incomplete")  # a resume does not run these again
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


def _load_state(step: dict[str, Any], answer: str | None, kickoff: str) -> StepState:
    """A fresh state, or the saved one: after a pause (needs the answer) or after a crash."""
    checkpoint = step.get("checkpoint")
    if checkpoint is None:
        return StepState(messages=[Message(role="user", content=kickoff)])
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


def _tally_gaps(state: StepState) -> str:
    added = drafts = 0
    for call in state.tool_calls:
        try:
            if call.tool == "create_gap":
                CreateGapOutput.model_validate_json(call.output)
                added += 1
            elif call.tool == "draft_tenant_message":
                drafts += DraftTenantMessageOutput.model_validate_json(call.output).saved
        except ValueError:
            continue  # the call was a ToolError, so it changed nothing
    return f"{added} gaps added, {drafts} tenant drafts"


def _tally_draft(state: StepState) -> str:
    filled = rejected = 0
    for call in state.tool_calls:
        if call.tool != "fill_form_field":
            continue
        try:
            filled += FillFormFieldOutput.model_validate_json(call.output).filled
        except ValueError:
            rejected += 1  # a ToolError: a rejected citation or value, nothing saved
    return f"{filled} fields filled, {rejected} attempts rejected"


def _fillable_fields(session: Session, case: CaseRow) -> list[tuple[str, str]]:
    """(key, label) of the fields the draft step may fill (`tools.is_fillable`): still missing,
    not the remedy, and not a draft the caseworker cleared."""
    rows = session.exec(
        select(FormFieldRow)
        .where(FormFieldRow.case_id == case.id)
        .order_by(col(FormFieldRow.position))
    ).all()
    covered_fields = covered_legacy_fields(rows)
    return [
        (row.key, row.label) for row in rows
        if is_fillable(row) and row.key not in covered_fields
    ]


def _untouched(event: TimelineEventRow) -> bool:
    """Nobody worked on this event: not reviewed, no caseworker note, no date a caseworker set. A
    new run may replace it. (A title or description edit that was undone leaves no trace.)"""
    return (
        event.review_status == "pending"
        and not event.caseworker_note
        and (event.date or {}).get("basis") != "user"
    )


def _start_timeline(session: Session, case: CaseRow) -> tuple[list[str], list[str]]:
    """What a fresh timeline step starts from: one line per event a caseworker worked on, for the
    kickoff, and the ids of the others, an earlier run's proposals. This run proposes those afresh
    from all the evidence and they are removed when it finishes (`_replace_old_events`), so a
    failed run costs nothing. Without this every run added a full new set of copies."""
    rows = session.exec(
        select(TimelineEventRow)
        .where(TimelineEventRow.case_id == case.id)
        .order_by(col(TimelineEventRow.sort_key), col(TimelineEventRow.id))
    ).all()
    labels: dict[str, str] = dict(
        session.exec(
            select(EvidenceRow.id, EvidenceRow.label).where(EvidenceRow.case_id == case.id)
        ).all()
    )
    lines: list[str] = []
    replaces: list[str] = []
    for e in rows:
        if _untouched(e):
            replaces.append(e.id)
            continue
        cited = ", ".join(
            f"{labels.get(str(s.get('evidence_id')), '?')} {s.get('locator')}" for s in e.sources
        )
        when = (e.date or {}).get("value") or "no date"
        state = "kept" if e.review_status == "pending" else e.review_status
        lines.append(f"- {e.id} [{state}] {when}: {clean_text(e.title, 200)} ({cited})")
    return lines, replaces


def _delete_untouched(session: Session, case: CaseRow, ids: list[str]) -> list[TimelineEventRow]:
    """Delete those events unless a caseworker got to them meanwhile. The rows are locked and read
    again, so an Accept that was first is seen and one that comes later waits."""
    if not ids:
        return []
    stmt = (
        select(TimelineEventRow)
        .where(TimelineEventRow.case_id == case.id, col(TimelineEventRow.id).in_(ids))
        .with_for_update()
    )
    rows = session.exec(stmt.execution_options(populate_existing=True)).all()
    gone = [e for e in rows if _untouched(e)]
    for event in gone:
        session.delete(event)
    session.flush()
    return gone


def _events_word(count: int) -> str:
    return f"{count} unreviewed {'event' if count == 1 else 'events'}"


def _replace_old_events(session: Session, case: CaseRow, step: dict[str, Any]) -> None:
    """The timeline step finished: its proposals replace the earlier run's. Caller commits."""
    gone = _delete_untouched(session, case, step.pop("replaces", []))
    if gone:
        activity.log(
            session, case.id, "agent", AGENT_ACTOR,
            f"Removed {_events_word(len(gone))} from an earlier run: the new timeline replaces "
            "them",
        )  # fmt: skip
        gaps.remove_support_gaps(session, case, gone)


def _proposed_ids(step: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for call in step["tool_calls"]:
        if call["tool"] != "propose_event":
            continue
        try:
            event_id = ProposeEventOutput.model_validate_json(call["output"]).event_id
        except ValueError:
            continue  # the call was a ToolError, not a proposal
        if event_id:
            ids.append(event_id)
    return ids


def _keep_old_events(session: Session, case: CaseRow, step: dict[str, Any]) -> None:
    """The timeline step will not finish. The earlier run's events stay and this run's partial
    proposals go, so the two sets never mix. With no earlier events the partial ones are all the
    case has, and they stay. Caller commits."""
    if not step.pop("replaces", None):
        return
    gone = _delete_untouched(session, case, _proposed_ids(step))
    if gone:
        activity.log(
            session, case.id, "agent", AGENT_ACTOR,
            f"Dropped {_events_word(len(gone))} from an analysis that did not finish",
        )  # fmt: skip
    step["summary"] = f"{step['summary']} {OLD_EVENTS_KEPT}"


def _has_text(session: Session, case: CaseRow, texts: EvidenceTextStore) -> bool:
    """Whether any evidence item of the case has text the agent could cite."""
    ids = session.exec(select(EvidenceRow.id).where(EvidenceRow.case_id == case.id)).all()
    return any(texts.pages(case.id, evidence_id) for evidence_id in ids)


# What a step changed, for its summary; the timeline's counts are the default.
_TALLIES: dict[str, Callable[[StepState], str]] = {"gaps": _tally_gaps, "draft": _tally_draft}


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
    "finished": "done", "budget": "incomplete", "needs_input": "needs_input",
    "refused": "failed", "llm_error": "failed",
}  # fmt: skip


def _apply_result(
    step: dict[str, Any], state: StepState, result: LoopResult, tally: Callable[[StepState], str]
) -> None:
    status = _STEP_STATUS[result.status]
    summary = result.summary
    if status != "needs_input":
        summary = f"{summary} ({tally(state)})"
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
    """CONTRACT_CHANGES #4: failed if a step failed; needs_review if a step needs input or stopped
    early, an event is pending or a blocking gap is open; else done."""
    statuses = {s["status"] for s in run.steps}
    if "failed" in statuses:
        return "failed"
    blocking, pending = case_counts(session, run.case_id)
    if statuses & {"needs_input", "incomplete"} or blocking or pending:
        return "needs_review"
    return "done"


def _paused_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    for step in steps:
        if (
            step["key"] in IMPLEMENTED_STEPS and step["status"] == "needs_input"
            and (step.get("checkpoint") or {}).get("pending")
        ):  # fmt: skip
            return step
    return None


def paused_question(run: RunRow) -> dict[str, Any] | None:
    """The saved pause of a run waiting for the caseworker: the ask_caseworker tool_use id, the
    other calls' results and the question's field key (or None). None when nothing the runner
    can resume is waiting, for example a seeded run whose gaps step says `needs_input`."""
    if run.status != "needs_review":
        return None
    step = _paused_step(run.steps)
    return step["checkpoint"]["pending"] if step else None


def close_paused_runs(session: Session, case: CaseRow) -> None:
    """A new run replaces the runs of the case still waiting for an answer: resumed later, one of
    them would add its events to the new timeline. The rows are locked, so of an answer and a new
    run only the first gets through; the answer then gets "not waiting". Caller commits."""
    stmt = (
        select(RunRow)
        .where(RunRow.case_id == case.id, RunRow.status == "needs_review")
        .with_for_update()
    )
    for run in session.exec(stmt.execution_options(populate_existing=True)).all():
        steps = copy.deepcopy(run.steps)
        step = _paused_step(steps)
        if step is None:
            continue
        synthetic = step["checkpoint"]["pending"].get("synthetic")
        step["status"], step["summary"] = "failed", RUN_CLOSED
        del step["checkpoint"]
        if step["key"] == "timeline" and not synthetic:  # a synthetic pause: the step had finished
            _keep_old_events(session, case, step)
        run.steps = steps
        run.status = "failed"
        run.finished_at = datetime.now(UTC)
        session.add(run)
        activity.log(session, case.id, "agent", AGENT_ACTOR, f"Closed {run.id}: {RUN_CLOSED}")


def last_started(run: RunRow) -> datetime:
    """When the run last began working: its start, or the answer that resumed it."""
    resumed = (run.usage or {}).get("resumed_at")
    return datetime.fromisoformat(resumed) if resumed else run.started_at


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
    # Not in the API's usage. A slow answer must not make the resumed run look abandoned.
    row.usage = {**(row.usage or {}), "resumed_at": datetime.now(UTC).isoformat()}
    session.add(row)
    session.commit()


@dataclass(frozen=True)
class AgentStep:
    """The four things that differ between the agent-loop steps: how the conversation starts, the
    cached system prompt, the per-run note after it, and the tools. `tally` summarises what the
    step changed for the run page."""

    kickoff: str
    system: str
    run_note: str
    tools: list[str]
    tally: Callable[[StepState], str]


def _agent_step(key: str, ctx: ToolContext, kickoff: str) -> AgentStep:
    if key == "draft":
        return AgentStep(
            kickoff=kickoff, system=prompts.draft_system(),
            run_note=prompts.draft_run_note(ctx.nonce), tools=prompts.DRAFT_TOOLS,
            tally=_tally_draft,
        )  # fmt: skip
    if key == "gaps":
        return AgentStep(
            kickoff=kickoff, system=prompts.gaps_system(),
            run_note=prompts.gaps_run_note(ctx.nonce), tools=prompts.GAPS_TOOLS,
            tally=_tally_gaps,
        )  # fmt: skip
    return AgentStep(
        kickoff=kickoff, system=prompts.timeline_system(),
        run_note=prompts.timeline_run_note(ctx.nonce), tools=prompts.TIMELINE_TOOLS,
        tally=_tally,
    )  # fmt: skip


async def _run_agent_step(
    step: AgentStep, key: str, ctx: ToolContext, provider: LLMProvider, state: StepState,
    on_turn: TurnHook,
) -> LoopResult:  # fmt: skip
    return await run_agent_loop(
        provider, ctx, state, step_key=key, system=step.system, system_run=step.run_note,
        tool_names=step.tools, on_turn=on_turn,
    )  # fmt: skip


def _step(all_steps: list[dict[str, Any]], key: str) -> dict[str, Any]:
    return next(s for s in all_steps if s["key"] == key)


def _needs_hearing_ask(case: CaseRow, state: StepState, result: LoopResult) -> bool:
    """Ask for the hearing date when the step ended normally, the case has none, and the model
    did not ask anything itself (one question per step, not two)."""
    if result.status not in ("finished", "budget") or case.hearing_date is not None:
        return False
    if case.no_hearing_yet:  # a caseworker already said there is none (#124)
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
        "summary": step["summary"], "status": step["status"],
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
    deadline_s: float | None = None,
    answer: str | None = None,
    steps: tuple[str, ...] = IMPLEMENTED_STEPS,
    claimed: bool = False,
) -> RunRow:
    """Run the given steps that are not done yet. The deadline covers all of them together; on a
    resume it is what is left of the budget after the time earlier calls already used. The
    nonce is chosen once and kept on the steps: a resume reuses it, and passing a different one
    is an error. `deadline_s` defaults to the `RUN_BUDGET_S` setting."""
    if deadline_s is None:
        deadline_s = get_settings().run_budget_s
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
                if step["status"] in STEP_OVER:
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
                if key == "rules":
                    # Deterministic, no model call: one go, no checkpoint to resume from.
                    state = StepState(messages=[])
                    current = (step, state)
                    step["nonce"] = ctx.nonce
                    _write_extract(step, state, "running", None)
                    run.steps = copy.deepcopy(all_steps)
                    session.commit()
                    result = legal_rules.run(session, case)
                    state.tool_calls.append(
                        ToolCall(tool="rules", input="{}", output=result.tool_call_output())
                    )
                    _write_extract(step, state, "done", result.summary)
                    current = None
                    run.steps = copy.deepcopy(all_steps)
                    session.commit()
                    continue
                saved = (step.get("checkpoint") or {}).get("pending") or {}
                kickoff = prompts.TIMELINE_KICKOFF
                # Only on a fresh start: a step resumed after a question or a crash goes on with
                # its conversation and the list saved on the step.
                if key == "timeline" and step.get("checkpoint") is None:
                    reviewed, step["replaces"] = _start_timeline(session, case)
                    kickoff = prompts.timeline_kickoff(tuple(reviewed), ctx.nonce)
                if key == "gaps":
                    # The deterministic gaps are written before the agent reads anything, so it
                    # is told what already exists instead of inventing duplicates.
                    prepared = gaps.prepare(session, case)
                    session.commit()
                    kickoff = prompts.gaps_kickoff(prepared.open_gaps, prepared.language, ctx.nonce)
                # Only on a fresh start: a step resumed from its checkpoint (after a crash) goes
                # on with its conversation, whatever it already filled.
                if key == "draft" and step.get("checkpoint") is None:
                    gaps.ensure_form_fields(session, case)  # a no-op once gaps has run
                    fillable = _fillable_fields(session, case)
                    skip = (
                        "No missing form fields to fill." if not fillable
                        else None if _has_text(session, case, texts)
                        else "No evidence text to fill the form from yet."
                    )  # fmt: skip
                    if skip is not None:  # no model call
                        state = StepState(messages=[])
                        step["nonce"] = ctx.nonce
                        _write_step(step, state, "done", skip)
                        run.steps = copy.deepcopy(all_steps)
                        session.commit()
                        continue
                    kickoff = prompts.draft_kickoff(fillable)
                spec = _agent_step(key, ctx, kickoff)
                state = _load_state(step, answer, spec.kickoff)
                current = (step, state)
                step["nonce"] = ctx.nonce
                if saved.get("synthetic"):  # the hearing-date answer: the step is complete
                    _write_step(step, state, saved.get("status", "done"), saved["summary"])
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

                result = await _run_agent_step(spec, key, ctx, provider, state, on_turn)
                _apply_result(step, state, result, spec.tally)
                if key == "timeline":
                    if result.status == "finished":
                        _replace_old_events(session, case, step)
                    elif result.status != "needs_input":
                        _keep_old_events(session, case, step)
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
            tally = _TALLIES.get(step["key"], _tally)(state)
            why = f"Stopped early: the {deadline_s:.0f} s run deadline passed ({tally})."
            _write_step(step, state, "incomplete", why)
            if step["key"] == "timeline":
                _keep_old_events(session, case, step)
    except asyncio.CancelledError:
        session.rollback()  # the step stays running with its last checkpoint: it can resume
        raise
    except Exception as exc:  # a failed step must not leave the run "running" forever
        session.rollback()
        log.warning("run %s step failed: %s", run.id, type(exc).__name__)  # type only
        if current is not None:
            step, state = current
            _write_step(step, state, "failed", f"The step failed ({type(exc).__name__}).")
            if step["key"] == "timeline":
                _keep_old_events(session, case, step)
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
