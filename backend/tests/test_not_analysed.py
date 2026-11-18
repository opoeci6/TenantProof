"""A case nobody has analysed must not look finished: the package is refused (409, plain
reason) without a completed run or without an accepted or edited event, and the caseload says
whether a case has been analysed (`analysed`), so the UI can say "Not analysed yet"."""

from datetime import UTC, datetime

import pytest
from sqlmodel import Session, col, delete, select

from app.models import RunRow, TimelineEventRow
from app.services.package import NOT_ANALYSED, NOTHING_REVIEWED
from tests.test_package import ATTESTED, BUILD, CASE, make_ready

LIVE = "case_live_001"  # seeded with no run, no events and no form fields


def _drop_runs(session: Session, case_id: str = CASE) -> None:
    session.exec(delete(RunRow).where(col(RunRow.case_id) == case_id))  # type: ignore[call-overload]
    session.commit()


def _set_reviews(session: Session, status: str, case_id: str = CASE) -> None:
    for event in session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == case_id)):
        event.review_status = status
        session.add(event)
    session.commit()


def _add_run(session: Session, status: str, *, finished: bool, case_id: str = CASE) -> None:
    session.add(RunRow(
        id=f"run_t_{status}_{finished}", case_id=case_id, status=status,
        started_at=datetime.now(UTC), finished_at=datetime.now(UTC) if finished else None,
        steps=[],
    ))
    session.commit()


def _reasons(client) -> list[str]:
    r = client.post(BUILD, json=ATTESTED)
    assert r.status_code == 409, r.text
    return r.json()["detail"]["reasons"]


def _analysed(client, case_id: str) -> bool | None:
    rows = {c["id"]: c for c in client.get("/api/cases").json()}
    return rows[case_id]["analysed"]


# --------------------------------------------------------------------------- the package


def test_the_seeded_demo_case_still_builds(fresh_db: None, client) -> None:
    make_ready(client)
    assert client.post(BUILD, json=ATTESTED).status_code == 200


def test_no_completed_run_refuses_the_package_with_a_plain_reason(
    fresh_db: None, client, session: Session
) -> None:
    make_ready(client)
    _drop_runs(session)
    assert _reasons(client) == [NOT_ANALYSED]


@pytest.mark.parametrize(("status", "finished"), [
    ("running", False), ("needs_review", False), ("failed", True),
])
def test_a_run_that_is_paused_running_or_failed_is_not_a_completed_run(
    fresh_db: None, client, session: Session, status: str, finished: bool
) -> None:
    make_ready(client)
    _drop_runs(session)
    _add_run(session, status, finished=finished)
    assert _reasons(client) == [NOT_ANALYSED]


def test_a_finished_run_that_still_needs_review_counts(
    fresh_db: None, client, session: Session
) -> None:
    make_ready(client)
    _drop_runs(session)
    _add_run(session, "needs_review", finished=True)
    assert client.post(BUILD, json=ATTESTED).status_code == 200


@pytest.mark.parametrize("status", ["pending", "rejected"])
def test_no_accepted_or_edited_event_refuses_the_package(
    fresh_db: None, client, session: Session, status: str
) -> None:
    make_ready(client)
    _set_reviews(session, status)
    assert _reasons(client) == [NOTHING_REVIEWED]


def test_an_edited_event_is_enough(fresh_db: None, client, session: Session) -> None:
    make_ready(client)
    _set_reviews(session, "pending")
    first = session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)).first()
    assert first is not None
    first.review_status = "edited"
    session.add(first)
    session.commit()
    assert client.post(BUILD, json=ATTESTED).status_code == 200


def test_both_reasons_are_listed_when_both_are_missing(
    fresh_db: None, client, session: Session
) -> None:
    make_ready(client)
    _drop_runs(session)
    _set_reviews(session, "pending")
    assert _reasons(client) == [NOT_ANALYSED, NOTHING_REVIEWED]


def test_the_attestation_check_still_comes_first(
    fresh_db: None, client, session: Session
) -> None:
    _drop_runs(session)
    r = client.post(BUILD, json={"attestations": {}})
    assert r.status_code == 422  # CONTRACT_CHANGES #8: the body first, then the case state


def test_an_empty_live_case_is_refused(fresh_db: None, client) -> None:
    r = client.post(f"/api/cases/{LIVE}/package", json=ATTESTED)
    assert r.status_code == 409
    reasons = r.json()["detail"]["reasons"]
    assert NOT_ANALYSED in reasons and NOTHING_REVIEWED in reasons


# --------------------------------------------------------------------------- the caseload


def test_the_caseload_marks_a_case_with_no_run_and_no_reviewed_event(
    fresh_db: None, client
) -> None:
    assert _analysed(client, LIVE) is False
    assert _analysed(client, CASE) is True  # a finished run and two accepted events


def test_a_completed_run_alone_makes_a_case_analysed(
    fresh_db: None, client, session: Session
) -> None:
    _add_run(session, "done", finished=True, case_id=LIVE)
    assert _analysed(client, LIVE) is True


def test_a_reviewed_event_alone_makes_a_case_analysed(
    fresh_db: None, client, session: Session
) -> None:
    _drop_runs(session)
    assert _analysed(client, CASE) is True  # its two accepted events
    _set_reviews(session, "pending")
    assert _analysed(client, CASE) is False


def test_an_unfinished_run_does_not_make_a_case_analysed(
    fresh_db: None, client, session: Session
) -> None:
    _add_run(session, "needs_review", finished=False, case_id=LIVE)
    assert _analysed(client, LIVE) is False
