import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db import get_engine
from app.deps import CaseDep, SessionDep, UserDep, load_owned
from app.models import CaseRow, RunRow
from app.schemas import Run
from app.services import activity
from app.services.agent import llm, runner
from app.services.agent.evidence_text import SqlTextStore
from app.services.dates import (
    evidence_deadline,
    hearing_has_passed,
    is_no_hearing_answer,
    parse_hearing_date,
)
from app.services.legal.corpus import EVIDENCE_DAYS_BEFORE_HEARING
from app.services.mappers import to_run
from app.services.stage import refresh_stage

router = APIRouter()
log = logging.getLogger(__name__)

MAX_ANSWER_CHARS = 500
DATE_HINT = "Enter the date like 2026-11-21 or Nov 21, 2026"
DATE_PASSED = "That date has passed. Enter the upcoming hearing date."
NO_HEARING_TEXT = "No hearing is scheduled yet. Do not ask for a hearing date again."
# A run still "running" after its whole budget (plus slack for a slow start) was cut off by a
# restart or a crash, so it no longer blocks a new one.
STALE_SLACK = timedelta(seconds=30)


def stale_after() -> timedelta:
    return timedelta(seconds=get_settings().run_budget_s) + STALE_SLACK


class AnswerIn(BaseModel):
    """Request side of POST /runs/{id}/answer. A missing answer counts as empty, so the handler
    can answer 422 with { detail: { reasons } } (API_CONTRACT) instead of FastAPI's default."""

    answer: str = ""


def _today() -> date:
    """Today in UTC. A function so tests can fix the date."""
    return datetime.now(UTC).date()


def provider_dep() -> llm.LLMProvider:
    """The model provider for this deployment (`LLM_MODE`). Tests override this dependency."""
    try:
        return llm.get_provider(get_settings())
    except llm.LLMConfigError as exc:
        log.warning("analysis unavailable: %s", exc)  # the message names the env var, not a key
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Analysis is not set up on this server"
        ) from exc


ProviderDep = Annotated[llm.LLMProvider, Depends(provider_dep)]


def _reasons(code: int, *reasons: str) -> HTTPException:
    return HTTPException(code, {"reasons": list(reasons)})


def _resume(run_id: str, answer: str, provider: llm.LLMProvider) -> None:
    """Background task: resume the paused step in its own session and event loop. The pause was
    already claimed by the route. The runner records its own failures on the run; this only
    catches what is left, so the run never stays "running" forever."""
    with Session(get_engine()) as session:
        try:
            row = session.get(RunRow, run_id)
            if row is None:
                return
            asyncio.run(runner.execute_run(
                session, row, provider, SqlTextStore(session), answer=answer, claimed=True))
        except Exception as exc:
            session.rollback()
            log.warning("run %s failed to resume: %s", run_id, type(exc).__name__)  # type only
            row = session.get(RunRow, run_id)
            if row is not None and row.status == "running":
                row.status = "failed"
                session.add(row)
                session.commit()


def _execute(run_id: str, provider: llm.LLMProvider) -> None:
    """Background task: its own session and event loop, so the API stays responsive. The
    runner records its own failures on the run; this only catches what is left."""
    with Session(get_engine()) as session:
        try:
            row = session.get(RunRow, run_id)
            if row is None:
                return
            asyncio.run(runner.execute_run(session, row, provider, SqlTextStore(session)))
        except Exception as exc:  # never leave the run "running" forever
            session.rollback()
            log.warning("run %s failed to execute: %s", run_id, type(exc).__name__)
            row = session.get(RunRow, run_id)
            if row is not None and row.status == "running":
                row.status = "failed"
                row.finished_at = datetime.now(UTC)
                session.add(row)
                session.commit()


@router.post("/cases/{case_id}/runs", response_model=Run)
def start_run(
    case: CaseDep, session: SessionDep, background: BackgroundTasks, provider: ProviderDep
) -> Run:
    """Starts the analysis and returns at once with `status: "running"`; the UI polls
    `GET /runs/{id}`. The case comes from the path and the caseworker's org, never a body."""
    if case.applicant_role == "landlord":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {"reasons": ["Landlord applications are drafted manually in Form draft"]},
        )
    # Lock the case row so two clicks cannot both pass the "already running" check.
    session.exec(select(CaseRow).where(CaseRow.id == case.id).with_for_update()).one()
    # Before the check: a pause answered first is "running" by then, and blocks this run. If the
    # check fails, nothing is committed and the paused run stays as it was.
    runner.close_paused_runs(session, case)
    cutoff = datetime.now(UTC) - stale_after()
    running = session.exec(
        select(RunRow).where(RunRow.case_id == case.id, RunRow.status == "running")
    ).all()
    if any(runner.last_started(row) > cutoff for row in running):
        raise HTTPException(
            status.HTTP_409_CONFLICT, {"reasons": ["An analysis is already running for this case"]}
        )
    row = runner.create_run(session, case)  # logs "Started run_..." and commits
    background.add_task(_execute, row.id, provider)
    return to_run(row)


def _clear_hearing(session: Session, case: CaseRow) -> None:
    """"No hearing scheduled yet": no date and no evidence deadline, and the case leaves
    `hearing_scheduled` if an earlier date had put it there. The flag stops the runner asking
    again on the next run."""
    case.hearing_date = None
    case.next_deadline = None
    case.no_hearing_yet = True
    if case.stage == "hearing_scheduled":
        case.stage = "needs_review"
        refresh_stage(session, case)  # from the case's own counts, like any other change
    session.add(case)


@router.post("/runs/{run_id}/answer", response_model=Run)
def answer_run(
    run_id: str, body: AnswerIn, session: SessionDep, user: UserDep,
    background: BackgroundTasks, provider: ProviderDep,
) -> Run:
    """Answer the question a paused run asked and resume it in the background. The run comes
    from the path and the caseworker's org, never from the body."""
    row = load_owned(RunRow, run_id, session, user)  # 404 for another org's run
    answer = body.answer.strip()
    if not answer or len(answer) > MAX_ANSWER_CHARS:
        raise _reasons(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"Type an answer of up to {MAX_ANSWER_CHARS} characters",
        )
    pending = runner.paused_question(row)
    if pending is None:
        raise _reasons(status.HTTP_409_CONFLICT, "This run is not waiting for an answer")
    if not runner.can_resume_with(row, provider):
        raise _reasons(
            status.HTTP_409_CONFLICT,
            "Restore the model configuration used for this run or start a new analysis",
        )
    text = answer
    hearing = None
    no_hearing = False
    if pending.get("field") == "hearing_date":
        no_hearing = is_no_hearing_answer(answer)
        if no_hearing:
            text = NO_HEARING_TEXT  # the agent is told there is no date, and not to ask again
        else:
            hearing = parse_hearing_date(answer)
            if hearing is None:  # the run stays paused: nothing has been changed yet
                raise _reasons(status.HTTP_422_UNPROCESSABLE_CONTENT, DATE_HINT)
            if hearing_has_passed(hearing, _today()):  # the run stays paused, nothing changes
                raise _reasons(status.HTTP_422_UNPROCESSABLE_CONTENT, DATE_PASSED)
            text = hearing.isoformat()  # the agent is told the date in its stored form
    case = session.get(CaseRow, row.case_id)
    assert case is not None  # load_owned checked it
    if hearing is not None:
        deadline = evidence_deadline(hearing, EVIDENCE_DAYS_BEFORE_HEARING)
        case.hearing_date = hearing.isoformat()
        case.stage = "hearing_scheduled"  # compute_stage keeps it from here on
        case.next_deadline = deadline  # what the caseload priority reads
        case.no_hearing_yet = False
        session.add(case)
        activity.log(session, case.id, "caseworker", user.name,
                     f"Set the hearing date to {hearing.isoformat()} "
                     f"(must be received by {deadline['date']})")
    if no_hearing:
        _clear_hearing(session, case)
        activity.log(session, case.id, "caseworker", user.name,
                     "Set the hearing as not scheduled yet (no evidence deadline)")
    activity.log(session, case.id, "caseworker", user.name,
                 f"Answered the agent's question on {row.id}")
    try:
        # Commits the claim with the date and the log above. If another answer got here first,
        # this rolls them back, so a second answer never changes the date.
        runner.claim_pause(session, row)
    except runner.RunNotWaiting:
        raise _reasons(
            status.HTTP_409_CONFLICT, "This question has already been answered"
        ) from None
    background.add_task(_resume, row.id, text, provider)
    return to_run(row)


@router.get("/cases/{case_id}/runs/latest", response_model=Run)
def latest_run(case: CaseDep, session: SessionDep) -> Run:
    row = session.exec(
        select(RunRow).where(RunRow.case_id == case.id).order_by(col(RunRow.started_at).desc())
    ).first()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No runs")
    return to_run(row)


@router.get("/runs/{run_id}", response_model=Run)
def get_run(run_id: str, session: SessionDep, user: UserDep) -> Run:
    return to_run(load_owned(RunRow, run_id, session, user))
