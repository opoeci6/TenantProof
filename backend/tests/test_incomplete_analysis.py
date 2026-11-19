"""A step cut off by a limit or the run deadline (#119): it ends `incomplete`, stays over on a
resume, and the case brief and the package say so. Scripted model only."""

import asyncio
import json
import re

import pytest
from sqlmodel import Session, delete, select

from app.models import CaseRow, EvidencePageRow, FormFieldRow, GapRow, RunRow, TimelineEventRow
from app.services.agent import llm, loop, runner
from app.services.case_brief import NEEDS
from app.services.package import (
    HINDI_NOTE,
    STOPPED_OTHER,
    STOPPED_TIMELINE,
    analysis_warning,
)
from tests.agent_support import CASE, RecordingProvider, demo_store, run, tool_use, turn
from tests.test_case_brief import add_event, brief, set_gaps_step, squash
from tests.test_package import build, download, make_ready, pdf_text, unzip

pytestmark = pytest.mark.usefixtures("hearing_known")

AGENT_STEPS = ["timeline", "draft", "gaps"]
FINISH = tool_use("finish", "f1", summary="All done.")
TIMELINE = "Build timeline"
GAPS = "Find missing information"


class Slow(RecordingProvider):
    """Every model call takes longer than the run deadline the test sets."""

    async def complete(self, request: llm.LLMRequest) -> llm.LLMResponse:
        await asyncio.sleep(1)
        return await super().complete(request)


def capped(key: str) -> dict[str, llm.LLMResponse]:
    """One turn asking for more tool calls than a step may make."""
    uses = [tool_use("list_evidence", f"u{i}") for i in range(loop.MAX_TOOL_CALLS + 1)]
    return {f"{key}-turn1": turn(*uses)}


def make_fillable(session: Session) -> None:
    """Give the draft step a field to work on, so it calls the model."""
    row = session.get(FormFieldRow, (CASE, "landlord_name"))
    assert row is not None
    row.value, row.status, row.sources, row.note = None, "missing", [], None
    session.add(row)
    session.commit()


def execute(
    session: Session, provider: llm.FixtureProvider, *steps: str, **kw: object
) -> RunRow:
    case = session.get(CaseRow, CASE)
    assert case is not None
    row = runner.create_run(session, case)
    return run(runner.execute_run(session, row, provider, demo_store(), steps=steps, **kw))  # type: ignore[arg-type]


def step_of(row: RunRow, key: str) -> dict:
    return next(s for s in row.steps if s["key"] == key)


def nothing_left(session: Session) -> None:
    """No event to review and no open gap."""
    for event in session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)):
        event.review_status = "rejected"
        session.add(event)
    for gap in session.exec(select(GapRow).where(GapRow.case_id == CASE)):
        gap.status = "resolved"
        session.add(gap)
    session.commit()


def stop(session: Session, *keys: str) -> None:
    """Mark steps of the seeded run as stopped early."""
    row = session.exec(select(RunRow).where(RunRow.case_id == CASE)).one()
    row.steps = [
        {**s, "status": "incomplete" if s["key"] in keys else "done"} for s in row.steps
    ]
    session.add(row)
    session.commit()


# --------------------------------------------------------------------------- the step


@pytest.mark.parametrize("key", AGENT_STEPS)
def test_a_step_at_the_tool_call_limit_ends_incomplete(
    fresh_db: None, client, session: Session, key: str
) -> None:
    make_fillable(session)
    row = execute(session, RecordingProvider(capped(key)), key)
    step = step_of(row, key)
    assert step["status"] == "incomplete"
    assert step["summary"].startswith(
        f"Stopped early: the step reached its limit of {loop.MAX_TOOL_CALLS} tool calls."
    )
    shown = client.get(f"/api/runs/{row.id}").json()
    assert next(s for s in shown["steps"] if s["key"] == key)["status"] == "incomplete"


@pytest.mark.parametrize("key", AGENT_STEPS)
def test_a_step_at_the_run_deadline_ends_incomplete(
    fresh_db: None, session: Session, key: str
) -> None:
    make_fillable(session)
    row = execute(session, Slow({}), key, deadline_s=0.1)
    step = step_of(row, key)
    assert step["status"] == "incomplete" and "run deadline passed" in step["summary"]


@pytest.mark.parametrize("key", AGENT_STEPS)
def test_a_step_at_the_token_budget_ends_incomplete(
    fresh_db: None, session: Session, key: str
) -> None:
    make_fillable(session)
    script = {f"{key}-turn1": turn(tool_use("list_evidence", "u1"), tokens=(150_000, 60_000))}
    step = step_of(execute(session, RecordingProvider(script), key), key)
    assert step["status"] == "incomplete" and "tokens" in step["summary"]


@pytest.mark.parametrize("key", AGENT_STEPS)
def test_a_step_that_finishes_ends_done(fresh_db: None, session: Session, key: str) -> None:
    make_fillable(session)
    row = execute(session, RecordingProvider({f"{key}-turn1": turn(FINISH)}), key)
    assert step_of(row, key)["status"] == "done"


def test_a_run_with_a_stopped_step_is_not_done_even_with_nothing_to_review(
    fresh_db: None, session: Session
) -> None:
    nothing_left(session)
    finished = execute(session, RecordingProvider({"timeline-turn1": turn(FINISH)}), "timeline")
    assert finished.status == "done"
    stopped = execute(session, RecordingProvider(capped("timeline")), "timeline")
    assert stopped.status == "needs_review" and stopped.finished_at is not None


def test_a_resume_does_not_run_a_stopped_step_again(fresh_db: None, session: Session) -> None:
    question = tool_use("ask_caseworker", "q1", question="Which unit?")
    first = RecordingProvider({**capped("timeline"), "gaps-turn1": turn(question)})
    row = execute(session, first, "timeline", "gaps")
    assert step_of(row, "gaps")["status"] == "needs_input"

    second = RecordingProvider({"gaps-turn2": turn(FINISH)})
    resumed = run(runner.execute_run(
        session, row, second, demo_store(), answer="4B", steps=("timeline", "gaps")))  # fmt: skip
    assert step_of(resumed, "timeline")["status"] == "incomplete"
    assert step_of(resumed, "gaps")["status"] == "done"
    assert [r.fixture_key for r in second.requests] == ["gaps-turn2"]


def test_the_hearing_date_answer_leaves_a_stopped_timeline_incomplete(
    fresh_db: None, session: Session
) -> None:
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.hearing_date = None
    session.add(case)
    session.commit()
    row = execute(session, RecordingProvider(capped("timeline")), "timeline")
    assert step_of(row, "timeline")["status"] == "needs_input"

    answered = run(runner.execute_run(
        session, row, RecordingProvider({}), demo_store(), answer="2026-11-24",
        steps=("timeline",),
    ))  # fmt: skip
    step = step_of(answered, "timeline")
    assert step["status"] == "incomplete" and step["summary"].startswith("Stopped early")


# --------------------------------------------------------------------------- the case brief


def counted(text: str) -> int:
    match = re.search(r"(\d+)items?needs?caseworkerverification", str(text))
    assert match, "no count line"
    return int(match.group(1))


def checklist(text: str) -> str:
    return str(text).split("5.Verificationchecklist", 1)[1]


def test_the_count_and_checklist_cover_every_open_gap_and_event_flag(
    fresh_db: None, client, session: Session
) -> None:
    add_event(session, flags=[{"type": "conflict", "message": "Sources disagree: the rent"}])
    gaps = session.exec(
        select(GapRow).where(GapRow.case_id == CASE, GapRow.status == "open")
    ).all()
    assert gaps
    text = brief(client)
    items = checklist(text)
    assert counted(text) == items.count("[]")
    assert all(squash(gap.title) in items for gap in gaps)
    assert squash("Sources disagree: the rent") in items
    assert "Nothing is flagged" not in text


@pytest.mark.parametrize(
    ("gaps_kept", "line", "nothing"),
    [(0, "0 items need caseworker verification", True),
     (1, "1 item needs caseworker verification", False)],
)  # fmt: skip
def test_nothing_is_flagged_only_when_the_list_is_empty(
    fresh_db: None, client, session: Session, gaps_kept: int, line: str, nothing: bool
) -> None:
    case = session.get(CaseRow, CASE)
    assert case is not None
    session.add(EvidencePageRow(
        evidence_id="ev_01", locator="page 99", case_id=CASE, position=99,
        text=f"{case.tenant_name} {case.unit}",
    ))  # fmt: skip
    session.exec(delete(TimelineEventRow).where(TimelineEventRow.case_id == CASE))  # type: ignore[call-overload]
    for index, gap in enumerate(session.exec(select(GapRow).where(GapRow.case_id == CASE)).all()):
        if index >= gaps_kept:
            session.delete(gap)
    session.commit()
    set_gaps_step(session, "done")
    text = brief(client)
    assert line in text
    assert ("Nothing is flagged" in text) is nothing


def test_the_brief_starts_with_the_timeline_warning(
    fresh_db: None, client, session: Session
) -> None:
    stop(session, "timeline", "gaps")
    text = brief(client)
    assert text.index(STOPPED_TIMELINE) < text.index("1. Case summary")
    assert f"Stopped early: {TIMELINE}, {GAPS}" in text
    assert STOPPED_OTHER not in text


def test_the_brief_warns_about_other_steps_in_other_words(
    fresh_db: None, client, session: Session
) -> None:
    stop(session, "gaps")
    text = brief(client)
    assert text.index(STOPPED_OTHER) < text.index("1. Case summary")
    assert f"Stopped early: {GAPS}" in text and STOPPED_TIMELINE not in text
    assert f"{NEEDS}: the gaps step stopped early" in text
    assert "the gaps step has not completed" not in text


def test_the_brief_has_no_warning_when_every_step_finished(
    fresh_db: None, client, session: Session
) -> None:
    stop(session)
    assert "The analysis stopped early" not in brief(client)


# --------------------------------------------------------------------------- the package


def built_package(client) -> tuple[dict, str]:
    make_ready(client)
    build(client)  # a stopped step warns; it does not block
    entries = unzip(download(client).content)
    return json.loads(entries["manifest.json"]), squash(pdf_text(entries["evidence_manifest.pdf"]))


def test_the_package_says_the_timeline_stopped_early(
    fresh_db: None, client, session: Session
) -> None:
    stop(session, "timeline")
    manifest, pdf = built_package(client)
    assert manifest["analysis_incomplete"] is True
    assert manifest["incomplete_steps"] == [TIMELINE]
    assert manifest["notes"] == [STOPPED_TIMELINE, f"Stopped early: {TIMELINE}"]
    assert squash(STOPPED_TIMELINE) in pdf and squash(f"Stopped early: {TIMELINE}") in pdf


def test_the_package_says_another_step_stopped_early(
    fresh_db: None, client, session: Session
) -> None:
    stop(session, "gaps")
    manifest, pdf = built_package(client)
    assert manifest["incomplete_steps"] == [GAPS]
    assert manifest["notes"] == [STOPPED_OTHER, f"Stopped early: {GAPS}"]
    assert squash(STOPPED_OTHER) in pdf and squash(STOPPED_TIMELINE) not in pdf


def test_a_package_from_a_finished_analysis_has_no_warning(
    fresh_db: None, client, session: Session
) -> None:
    stop(session)
    manifest, pdf = built_package(client)
    assert manifest["analysis_incomplete"] is False and manifest["incomplete_steps"] == []
    assert "notes" not in manifest and squash("The analysis stopped early") not in pdf


def test_only_the_latest_run_counts(fresh_db: None, client, session: Session) -> None:
    stop(session, "timeline")
    case = session.get(CaseRow, CASE)
    assert case is not None
    runner.create_run(session, case)
    manifest, _ = built_package(client)
    assert manifest["analysis_incomplete"] is False


def test_a_deadline_during_extract_warns_about_the_timeline_that_never_ran(
    fresh_db: None, client, session: Session
) -> None:
    row = execute(session, Slow({}), *runner.IMPLEMENTED_STEPS, deadline_s=0.1)
    assert step_of(row, "extract")["status"] == "incomplete"
    assert step_of(row, "timeline")["status"] == "pending"
    expected = [
        STOPPED_TIMELINE,
        "Stopped early: Extract facts",
        "Not run: Build timeline, Look up official rules, Draft form, Find missing information",
    ]
    assert analysis_warning(row.steps) == expected

    text = brief(client)
    assert all(line in text for line in expected)
    assert text.index(expected[2]) < text.index("1. Case summary")
    manifest, pdf = built_package(client)
    assert manifest["incomplete_steps"] == ["Extract facts"]
    assert manifest["notes"] == expected
    assert all(squash(line) in pdf for line in expected)


def test_a_gaps_only_stop_keeps_the_other_wording_with_no_not_run_line(
    fresh_db: None, client, session: Session
) -> None:
    script = {
        "timeline-turn1": turn(FINISH), "draft-turn1": turn(FINISH), **capped("gaps"),
    }  # fmt: skip
    row = execute(session, RecordingProvider(script), "timeline", "rules", "draft", "gaps")
    assert step_of(row, "timeline")["status"] == "done"
    assert step_of(row, "gaps")["status"] == "incomplete"
    expected = [STOPPED_OTHER, f"Stopped early: {GAPS}"]
    assert analysis_warning(row.steps) == expected

    text = brief(client)
    assert all(line in text for line in expected)
    assert "Not run:" not in text and STOPPED_TIMELINE not in text
    for gap in session.exec(select(GapRow).where(GapRow.case_id == CASE)):
        if gap.id not in ("gap_01", "gap_02"):
            gap.status = "resolved"
            session.add(gap)
    session.commit()
    manifest, pdf = built_package(client)
    assert manifest["notes"] == expected
    assert squash(STOPPED_OTHER) in pdf and squash("Not run:") not in pdf


def test_the_warning_comes_before_the_hindi_note(
    fresh_db: None, client, session: Session
) -> None:
    stop(session, "gaps")
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.tenant_language = "hi"
    session.add(case)
    session.commit()
    manifest, _ = built_package(client)
    assert manifest["notes"] == [STOPPED_OTHER, f"Stopped early: {GAPS}", HINDI_NOTE]
