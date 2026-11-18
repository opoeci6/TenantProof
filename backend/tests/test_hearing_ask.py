"""The hearing-date question is asked by the runner, not left to the model: when the timeline step
ends, the case has no hearing date and the model asked nothing itself, the step pauses with the
ask_caseworker pause (field `hearing_date`). FixtureProvider only, no network."""

import asyncio
from collections.abc import Iterator

import pytest
from sqlmodel import Session

from app.main import api
from app.models import CaseRow, RunRow
from app.routers import runs as runs_router
from app.services.agent import llm, runner
from app.services.agent.evidence_text import InMemoryTextStore
from tests.agent_support import CASE, RecordingProvider, tool_use, turn

URL = "/api/runs/{}/answer"
QUESTION = "What is the hearing date? It is not in the evidence."


def finishing() -> dict[str, llm.LLMResponse]:
    return {"timeline-turn1": turn(tool_use("finish", "u1", summary="Read the evidence."))}


def model_asks(field: str | None) -> dict[str, llm.LLMResponse]:
    extra = {"field": field} if field else {}
    return {
        "timeline-turn1": turn(tool_use("ask_caseworker", "u1", question="Hearing date?", **extra)),
        "timeline-turn2": turn(tool_use("finish", "u2", summary="Done after the answer.")),
    }


def set_hearing(session: Session, value: str | None) -> None:
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.hearing_date = value
    session.add(case)
    session.commit()


def start(session: Session, script: dict[str, llm.LLMResponse]) -> RunRow:
    """A timeline-step run, as the route would start it, on the demo case."""
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = runner.create_run(session, case)
    return asyncio.run(runner.execute_run(
        session, row, RecordingProvider(script), InMemoryTextStore(), steps=("timeline",)))


def timeline(row: RunRow) -> dict:
    return next(s for s in row.steps if s["key"] == "timeline")


def asks(row: RunRow) -> list[dict]:
    return [c for c in timeline(row)["tool_calls"] if c["tool"] == "ask_caseworker"]


@pytest.fixture
def resume() -> Iterator[RecordingProvider]:
    """The provider the answer route resumes with. Only the gaps step, which runs after the
    timeline on resume, is scripted: a timeline call would fail the test. The draft step after it
    makes no model call, because the case has no evidence text in the database here."""
    provider = RecordingProvider(
        {"gaps-turn1": turn(tool_use("finish", "g1", summary="Nothing new to add."))})
    api.dependency_overrides[runs_router.provider_dep] = lambda: provider
    yield provider
    api.dependency_overrides.pop(runs_router.provider_dep, None)


# --------------------------------------------------------------------------- the pause


def test_the_run_pauses_for_the_hearing_date_when_the_case_has_none(
    fresh_db: None, session: Session
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    step = timeline(row)
    assert step["status"] == "needs_input" and step["summary"] == QUESTION
    assert row.status == "needs_review" and row.finished_at is None
    [ask] = asks(row)
    assert '"field": "hearing_date"' in ask["input"] and QUESTION in ask["input"]
    pending = runner.paused_question(row)
    assert pending is not None and pending["field"] == "hearing_date"


def test_the_question_is_the_one_the_answer_route_already_handles(
    fresh_db: None, client, session: Session
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    steps = client.get(f"/api/runs/{row.id}").json()["steps"]
    step = next(s for s in steps if s["key"] == "timeline")
    assert step["status"] == "needs_input" and step["summary"] == QUESTION
    assert "checkpoint" not in str(step)  # the pause data stays on the server


def test_no_pause_when_the_case_has_a_hearing_date(fresh_db: None, session: Session) -> None:
    set_hearing(session, "2026-11-24")
    row = start(session, finishing())
    assert timeline(row)["status"] == "done" and not asks(row)
    assert row.finished_at is not None


def test_no_second_question_when_the_model_already_asked(
    fresh_db: None, client, session: Session
) -> None:
    set_hearing(session, None)
    row = start(session, model_asks("hearing_date"))
    assert timeline(row)["status"] == "needs_input" and timeline(row)["summary"] == "Hearing date?"
    assert len(asks(row)) == 1
    api.dependency_overrides[runs_router.provider_dep] = lambda: RecordingProvider(model_asks(None))
    try:
        r = client.post(URL.format(row.id), json={"answer": "Nov 24, 2026"})
    finally:
        api.dependency_overrides.pop(runs_router.provider_dep, None)
    assert r.status_code == 200
    session.expire_all()
    done = session.get(RunRow, row.id)
    assert done is not None and timeline(done)["status"] == "done"
    assert len(asks(done)) == 1  # the model's question was the only one


def test_a_model_question_without_the_field_still_counts_as_asked(
    fresh_db: None, client, session: Session
) -> None:
    """The model asked something else; the date is still empty after the answer, and the runner
    does not ask a second time."""
    set_hearing(session, None)
    row = start(session, model_asks(None))
    resumed = RecordingProvider({"timeline-turn2": model_asks(None)["timeline-turn2"]})
    api.dependency_overrides[runs_router.provider_dep] = lambda: resumed
    try:
        assert client.post(URL.format(row.id), json={"answer": "The 5th"}).status_code == 200
    finally:
        api.dependency_overrides.pop(runs_router.provider_dep, None)
    session.expire_all()
    done = session.get(RunRow, row.id)
    assert done is not None and timeline(done)["status"] == "done"
    assert len(asks(done)) == 1


def test_a_step_that_failed_does_not_ask(fresh_db: None, session: Session) -> None:
    set_hearing(session, None)
    row = start(session, {})  # no scripted reply: the model call fails
    assert timeline(row)["status"] == "failed" and not asks(row)
    assert row.status == "failed"


# --------------------------------------------------------------------------- the answer


def test_the_answer_sets_the_date_and_finishes_the_step_without_a_model_call(
    fresh_db: None, client, session: Session, resume: RecordingProvider
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    summary = "Read the evidence. (0 events proposed, 0 citations rejected)"

    r = client.post(URL.format(row.id), json={"answer": "Nov 24, 2026"})
    assert r.status_code == 200

    session.expire_all()
    case = session.get(CaseRow, CASE)
    assert case is not None and case.hearing_date == "2026-11-24"
    assert case.stage == "hearing_scheduled" and case.next_deadline is not None
    done = session.get(RunRow, row.id)
    assert done is not None
    step = timeline(done)
    assert step["status"] == "done" and step["summary"] == summary and "checkpoint" not in step
    assert [c["tool"] for c in step["tool_calls"]] == ["finish", "ask_caseworker"]
    assert done.finished_at is not None and runner.paused_question(done) is None
    # The timeline step needs no model call; the only request is the gaps step that follows.
    assert [request.fixture_key for request in resume.requests] == ["gaps-turn1"]


def test_a_bad_date_keeps_the_run_paused_and_the_date_empty(
    fresh_db: None, client, session: Session, resume: RecordingProvider
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    assert client.post(URL.format(row.id), json={"answer": "soon"}).status_code == 422
    session.expire_all()
    paused = session.get(RunRow, row.id)
    case = session.get(CaseRow, CASE)
    assert paused is not None and timeline(paused)["status"] == "needs_input"
    assert case is not None and case.hearing_date is None


def test_a_second_answer_is_refused_and_the_date_stays(
    fresh_db: None, client, session: Session, resume: RecordingProvider
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    assert client.post(URL.format(row.id), json={"answer": "2026-11-24"}).status_code == 200
    assert client.post(URL.format(row.id), json={"answer": "2026-12-01"}).status_code == 409
    session.expire_all()
    case = session.get(CaseRow, CASE)
    assert case is not None and case.hearing_date == "2026-11-24"


def test_an_empty_answer_is_refused(
    fresh_db: None, client, session: Session, resume: RecordingProvider
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    assert client.post(URL.format(row.id), json={"answer": "  "}).status_code == 422


def test_another_orgs_caseworker_cannot_answer(
    fresh_db: None, other_org_client, session: Session, resume: RecordingProvider
) -> None:
    set_hearing(session, None)
    row = start(session, finishing())
    r = other_org_client.post(URL.format(row.id), json={"answer": "2026-11-24"})
    assert r.status_code == 404
