"""POST /runs/{id}/answer: resume a run paused by ask_caseworker, store a hearing date, and
refuse a second answer. FixtureProvider only, so nothing reaches the network."""

import asyncio
import json
from collections.abc import Iterator
from datetime import date, timedelta

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.main import api
from app.models import ActivityRow, CaseRow, RunRow
from app.routers import runs as runs_router
from app.services.agent import llm, runner
from app.services.agent import tools as t
from app.services.agent.evidence_text import InMemoryTextStore
from app.services.dates import evidence_deadline, parse_hearing_date
from tests.agent_support import CASE, RecordingProvider, blocks, tool_use, turn

URL = "/api/runs/{}/answer"
QUESTION = "What is the hearing date?"


def ask(field: str | None = "hearing_date") -> llm.LLMResponse:
    extra = {"field": field} if field else {}
    return turn(tool_use("ask_caseworker", "u1", question=QUESTION, **extra))


def finish() -> llm.LLMResponse:
    return turn(tool_use("finish", "u2", summary="Done."))


def paused_run(session: Session, field: str | None = "hearing_date") -> RunRow:
    """A run the agent paused with a question, as the runner leaves it."""
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = runner.create_run(session, case)
    return asyncio.run(runner.execute_run(
        session, row, RecordingProvider({"timeline-turn1": ask(field)}), InMemoryTextStore()))


@pytest.fixture
def resume() -> Iterator[RecordingProvider]:
    """The provider the route uses to resume: it finishes the timeline in one turn, and the gaps
    step that runs after it adds nothing. The draft step makes no model call: the case has no
    evidence text in the database here."""
    provider = RecordingProvider({
        "timeline-turn2": finish(),
        "gaps-turn1": turn(tool_use("finish", "u3", summary="Nothing new to add.")),
    })
    api.dependency_overrides[runs_router.provider_dep] = lambda: provider
    yield provider
    api.dependency_overrides.pop(runs_router.provider_dep, None)


def case_row(session: Session) -> CaseRow:
    session.expire_all()
    case = session.get(CaseRow, CASE)
    assert case is not None
    return case


def hearing(session: Session) -> str | None:
    session.expire_all()
    case = session.get(CaseRow, CASE)
    assert case is not None
    return case.hearing_date


def activity_lines(session: Session) -> list[str]:
    session.expire_all()
    rows = session.exec(select(ActivityRow).where(ActivityRow.case_id == CASE)).all()
    return [a.action for a in rows]


def status_of(session: Session, run_id: str) -> tuple[str, str]:
    session.expire_all()
    run = session.get(RunRow, run_id)
    assert run is not None
    step = next(s for s in run.steps if s["key"] == "timeline")
    return run.status, step["status"]


# --------------------------------------------------------------------------- resume


def test_an_answer_resumes_the_run_and_the_agent_gets_it(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session)
    assert status_of(session, run.id) == ("needs_review", "needs_input")

    r = client.post(URL.format(run.id), json={"answer": "2026-11-21"})
    assert r.status_code == 200
    assert r.json()["status"] == "running" and r.json()["id"] == run.id

    assert status_of(session, run.id) == ("needs_review", "done")  # the background task has run
    first, *later = resume.requests
    assert first.fixture_key == "timeline-turn2"
    answer = next(b for b in blocks(first.messages[-1]) if b["tool_use_id"] == "u1")
    assert "2026-11-21" in answer["content"]  # the agent is told the stored date
    # the resume carries on into the steps after the pause: rules (no model call), gaps, and draft
    # (no model call without evidence text)
    assert [request.fixture_key for request in later] == ["gaps-turn1"]


def test_the_field_key_is_saved_with_the_pause_and_is_not_a_field_of_the_run(
    fresh_db, client, session: Session
) -> None:
    run = paused_run(session)
    pending = runner.paused_question(run)
    assert pending is not None and pending["field"] == "hearing_date"
    body = client.get(f"/api/runs/{run.id}").json()
    step = next(s for s in body["steps"] if s["key"] == "timeline")
    assert set(step) == {"key", "label", "status", "summary", "tool_calls"}  # no pause data
    assert "checkpoint" not in json.dumps(body) and "pending" not in step
    assert step["summary"] == QUESTION  # the question is the step's summary


def test_ask_caseworker_only_takes_the_defined_field_keys() -> None:
    tool = t.TOOLS["ask_caseworker"]
    assert not isinstance(t.validate_input(tool, {"question": "x", "field": "hearing_date"}),
                          t.ToolError)
    assert not isinstance(t.validate_input(tool, {"question": "x"}), t.ToolError)
    assert isinstance(t.validate_input(tool, {"question": "x", "field": "case_id"}), t.ToolError)


# --------------------------------------------------------------------------- hearing date


@pytest.mark.parametrize("typed", ["2026-11-21", "Nov 21, 2026", "21 November 2026"])
def test_a_hearing_date_is_saved_and_logged(
    fresh_db, client, session: Session, resume: RecordingProvider, typed: str
) -> None:
    run = paused_run(session)
    assert client.post(URL.format(run.id), json={"answer": typed}).status_code == 200
    assert hearing(session) == "2026-11-21"
    lines = activity_lines(session)
    assert "Set the hearing date to 2026-11-21 (must be received by 2026-11-16)" in lines
    assert f"Answered the agent's question on {run.id}" in lines


def test_a_bad_date_is_422_changes_nothing_and_the_run_stays_paused(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session)
    before = activity_lines(session)
    before_case = (case_row(session).stage, case_row(session).next_deadline)
    r = client.post(URL.format(run.id), json={"answer": "next Friday"})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == ["Enter the date like 2026-11-21 or Nov 21, 2026"]
    assert status_of(session, run.id) == ("needs_review", "needs_input")
    assert hearing(session) is None and activity_lines(session) == before
    assert (case_row(session).stage, case_row(session).next_deadline) == before_case
    assert resume.requests == []
    # The same question can be answered again.
    assert client.post(URL.format(run.id), json={"answer": "Nov 21, 2026"}).status_code == 200
    assert hearing(session) == "2026-11-21"
    assert status_of(session, run.id) == ("needs_review", "done")


PASSED = "That date has passed. Enter the upcoming hearing date."  # today is 2026-11-15 here


@pytest.mark.parametrize("typed", ["2026-11-14", "Nov 14, 2026", "14 November 2026", "2025-01-01"])
def test_a_date_that_has_passed_is_422_and_the_run_stays_paused(
    fresh_db, client, session: Session, resume: RecordingProvider, typed: str
) -> None:
    run = paused_run(session)
    before = activity_lines(session)
    before_case = (case_row(session).stage, case_row(session).next_deadline)
    r = client.post(URL.format(run.id), json={"answer": typed})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == [PASSED]
    assert status_of(session, run.id) == ("needs_review", "needs_input")
    assert hearing(session) is None and activity_lines(session) == before
    assert (case_row(session).stage, case_row(session).next_deadline) == before_case
    assert resume.requests == []
    # The same question can still be answered with an upcoming date.
    assert client.post(URL.format(run.id), json={"answer": "2026-11-20"}).status_code == 200
    assert hearing(session) == "2026-11-20"


@pytest.mark.parametrize("typed", ["2026-11-15", "2026-11-16", "Dec 1, 2026"])
def test_today_and_later_are_accepted(
    fresh_db, client, session: Session, resume: RecordingProvider, typed: str
) -> None:
    run = paused_run(session)
    assert client.post(URL.format(run.id), json={"answer": typed}).status_code == 200
    assert hearing(session) is not None


def test_the_past_date_rule_applies_to_the_hearing_field_only(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session, field=None)  # any other question keeps the answer as text
    assert client.post(URL.format(run.id), json={"answer": "2020-01-01"}).status_code == 200


def test_the_rule_is_a_pure_comparison() -> None:
    from datetime import date

    from app.services.dates import hearing_has_passed

    today = date(2026, 11, 15)
    assert hearing_has_passed(date(2026, 11, 14), today)
    assert not hearing_has_passed(today, today)
    assert not hearing_has_passed(date(2026, 11, 16), today)


def test_a_question_without_a_field_keeps_the_answer_as_text_and_writes_nothing(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session, field=None)
    assert client.post(URL.format(run.id), json={"answer": "Not a date at all"}).status_code == 200
    assert hearing(session) is None
    assert case_row(session).stage != "hearing_scheduled"
    block = next(b for b in blocks(resume.requests[0].messages[-1]) if b["tool_use_id"] == "u1")
    assert "Not a date at all" in block["content"]
    assert not any(line.startswith("Set the hearing date") for line in activity_lines(session))


@pytest.mark.parametrize("typed", ["2026-11-21", "Nov 21, 2026", "nov 21 2026", "21 November 2026",
                                   "21st Nov. 2026", "Sept 3, 2026", " 2026-11-21 "])
def test_valid_hearing_dates_parse(typed: str) -> None:
    assert isinstance(parse_hearing_date(typed), date)


@pytest.mark.parametrize("typed", ["", "next Friday", "11/21/2026", "2026-02-30", "2019-12-31",
                                   "2100-01-01", "Nov 2026", "21 Foo 2026", "Nov 31, 2026"])
def test_everything_else_is_not_a_hearing_date(typed: str) -> None:
    assert parse_hearing_date(typed) is None


# --------------------------------------------------------------------------- refusals


def test_a_second_answer_is_409(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session)
    assert client.post(URL.format(run.id), json={"answer": "2026-11-21"}).status_code == 200
    again = client.post(URL.format(run.id), json={"answer": "2027-01-05"})
    assert again.status_code == 409
    assert again.json()["detail"]["reasons"] == ["This run is not waiting for an answer"]
    assert hearing(session) == "2026-11-21"


def test_two_answers_that_race_for_one_pause_only_one_wins_and_only_it_writes(
    fresh_db, client, session: Session, resume: RecordingProvider, monkeypatch
) -> None:
    """Both requests passed the "is it paused" check; the claim lets one through. The loser's
    date and log lines are rolled back with its claim."""
    run = paused_run(session)
    pending = runner.paused_question(run)
    assert client.post(URL.format(run.id), json={"answer": "2026-11-21"}).status_code == 200
    monkeypatch.setattr(runner, "paused_question", lambda _run: pending)  # a stale read
    loser = client.post(URL.format(run.id), json={"answer": "2027-01-05"})
    assert loser.status_code == 409
    assert loser.json()["detail"]["reasons"] == ["This question has already been answered"]
    assert hearing(session) == "2026-11-21"
    assert case_row(session).next_deadline == evidence_deadline(date(2026, 11, 21))
    lines = activity_lines(session)
    assert sum(line.startswith("Set the hearing date") for line in lines) == 1
    assert sum(line.startswith("Answered the agent") for line in lines) == 1
    assert len(resume.requests) == 2  # the model was asked once for the timeline, once for gaps


def test_another_orgs_run_is_404_and_nothing_changes(
    fresh_db, other_org_client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session)
    before = activity_lines(session)
    r = other_org_client.post(URL.format(run.id), json={"answer": "2026-11-21"})
    assert r.status_code == 404
    assert status_of(session, run.id) == ("needs_review", "needs_input")
    assert hearing(session) is None and activity_lines(session) == before
    assert resume.requests == []


def test_unknown_run_is_404_and_signed_out_is_401(
    fresh_db, client, anon_client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session)
    assert client.post(URL.format("run_nope"), json={"answer": "x"}).status_code == 404
    assert anon_client.post(URL.format(run.id), json={"answer": "x"}).status_code == 401


def test_a_run_that_is_not_waiting_is_409(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    """The seeded run's gaps step says needs_input, but nothing the runner can resume is
    waiting, so there is no question to answer."""
    r = client.post(URL.format("run_001"), json={"answer": "2026-11-21"})
    assert r.status_code == 409 and hearing(session) is None


@pytest.mark.parametrize("body", [{"answer": ""}, {"answer": "   "}, {"answer": "x" * 501}, {}])
def test_an_empty_or_long_answer_is_422_and_the_run_stays_paused(
    fresh_db, client, session: Session, resume: RecordingProvider, body: dict
) -> None:
    run = paused_run(session)
    r = client.post(URL.format(run.id), json=body)
    assert r.status_code == 422 and "reasons" in r.json()["detail"]
    assert status_of(session, run.id) == ("needs_review", "needs_input")


def test_live_mode_without_a_key_is_503_and_the_run_stays_paused(
    fresh_db, client, session: Session, monkeypatch
) -> None:
    run = paused_run(session)
    monkeypatch.setattr(get_settings(), "llm_mode", "live")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = client.post(URL.format(run.id), json={"answer": "2026-11-21"})
    assert r.status_code == 503 and "ANTHROPIC" not in r.text
    assert status_of(session, run.id) == ("needs_review", "needs_input")
    assert hearing(session) is None


def test_a_failed_resume_marks_the_run_failed_not_running(
    fresh_db, client, session: Session
) -> None:
    run = paused_run(session)
    empty = RecordingProvider({})  # no reply for turn 2
    api.dependency_overrides[runs_router.provider_dep] = lambda: empty
    try:
        assert client.post(URL.format(run.id), json={"answer": "2026-11-21"}).status_code == 200
    finally:
        api.dependency_overrides.pop(runs_router.provider_dep, None)
    status, step = status_of(session, run.id)
    assert status == "failed" and step == "failed"


def test_a_long_text_answer_is_422_and_the_limit_is_500_characters(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    """With no field key the answer is only text, so the length limit is what refuses it."""
    run = paused_run(session, field=None)
    assert client.post(URL.format(run.id), json={"answer": "x" * 501}).status_code == 422
    assert status_of(session, run.id) == ("needs_review", "needs_input")
    assert client.post(URL.format(run.id), json={"answer": "x" * 500}).status_code == 200


# --------------------------------------------------------------------------- deadline


def test_a_hearing_date_sets_the_stage_and_the_evidence_deadline(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    run = paused_run(session)
    assert case_row(session).stage != "hearing_scheduled"
    assert client.post(URL.format(run.id), json={"answer": "2026-11-21"}).status_code == 200
    case = case_row(session)
    assert case.stage == "hearing_scheduled"  # and the resumed run's stage refresh kept it
    assert case.next_deadline == {
        "date": "2026-11-16", "label": "Must be received by · hearing 2026-11-21"}


def test_the_caseload_follows_the_new_deadline(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    """A hearing 8 days away puts the evidence deadline 3 days away: high priority, with that
    reason. The demo case also has required items open, so a far hearing is still high for
    that reason (the existing rule), but without a deadline in the reason."""
    near = date.today() + timedelta(days=8)
    run = paused_run(session)
    client.post(URL.format(run.id), json={"answer": near.isoformat()})
    [mine] = [c for c in client.get("/api/cases").json() if c["id"] == CASE]
    assert mine["stage"] == "hearing_scheduled" and mine["priority"] == "high"
    assert mine["next_deadline"]["date"] == (near - timedelta(days=5)).isoformat()
    assert "Must be received in 3 days" in mine["priority_reason"]


def test_a_far_hearing_is_scheduled_without_a_deadline_warning(
    fresh_db, client, session: Session, resume: RecordingProvider
) -> None:
    far = date.today() + timedelta(days=60)
    run = paused_run(session)
    client.post(URL.format(run.id), json={"answer": far.isoformat()})
    [mine] = [c for c in client.get("/api/cases").json() if c["id"] == CASE]
    assert mine["stage"] == "hearing_scheduled"
    assert "Must be received in" not in mine["priority_reason"]
    assert "open with a hearing scheduled" in mine["priority_reason"]  # existing rule 2


@pytest.mark.parametrize(("hearing_day", "due"), [
    (date(2026, 11, 21), "2026-11-16"), (date(2026, 3, 3), "2026-02-26"),
    (date(2026, 1, 3), "2025-12-29"), (date(2028, 3, 3), "2028-02-27"),
])
def test_evidence_is_due_five_days_before_the_hearing(hearing_day: date, due: str) -> None:
    assert evidence_deadline(hearing_day) == {
        "date": due, "label": f"Must be received by · hearing {hearing_day.isoformat()}"}
