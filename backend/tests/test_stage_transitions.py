"""Case stage and caseload order after mutations and the tenant submit (BUILD_PLAN section 6).
Stage is recomputed on every mutation by app.services.stage.refresh_stage; the caseload sort is
the pure priority.sort_key applied to each CaseSummary."""

from datetime import date
from typing import Any

from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.models import CaseRow, GapRow, TimelineEventRow
from app.services.priority import days_until, sort_key
from app.services.stage import refresh_stage

CASE = "case_demo_001"
HEARING_CASE = "case_002"
WAITING_REASON = "Intake link sent. No files from the tenant yet."
SUBMIT_BODY = {"attest_genuine": True, "consent_share": True, "language": "zh"}


def _demo(client: TestClient) -> dict[str, Any]:
    return next(c for c in client.get("/api/cases").json() if c["id"] == CASE)


def _caseload_key(summary: dict[str, Any]) -> tuple[int, int, int, int]:
    deadline = summary["next_deadline"]
    days = None if deadline is None else days_until(
        date.fromisoformat(deadline["date"]), date.today()
    )
    return sort_key(summary["priority"], days, summary["open_blocking"], summary["pending_review"])


def _assert_caseload_sorted(client: TestClient) -> list[dict[str, Any]]:
    cases = client.get("/api/cases").json()
    assert [c["id"] for c in cases] == [c["id"] for c in sorted(cases, key=_caseload_key)]
    return cases


def _request_demo_gap(client: TestClient, gap_id: str = "gap_01") -> None:
    r = client.post(f"/api/gaps/{gap_id}/tenant-request")
    assert r.status_code == 200


def _submit(anon_client: TestClient, token: str) -> None:
    r = anon_client.post(f"/api/intake/{token}/submit", json=SUBMIT_BODY)
    assert r.status_code == 200 and r.json() == {"ok": True}


# --- Waiting on the tenant -------------------------------------------------------------------


def test_tenant_request_moves_stage_to_waiting_tenant(fresh_db, client):
    _request_demo_gap(client)
    summary = _demo(client)
    assert summary["stage"] == "waiting_tenant"
    assert summary["priority_reason"] == WAITING_REASON
    assert summary["priority"] == "normal"


def test_accepting_event_while_waiting_keeps_waiting_tenant(fresh_db, client):
    _request_demo_gap(client)
    r = client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    assert r.status_code == 200
    summary = _demo(client)
    assert summary["stage"] == "waiting_tenant"
    assert summary["priority_reason"] == WAITING_REASON


def test_dismissing_the_waiting_gap_moves_stage_off_waiting_tenant(fresh_db, client):
    _request_demo_gap(client)
    client.patch("/api/gaps/gap_01", json={"status": "dismissed"})
    summary = _demo(client)
    assert summary["stage"] == "needs_review"  # gap_02 is still a blocking item
    assert summary["priority_reason"] != WAITING_REASON

    client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    assert _demo(client)["stage"] == "needs_review"


def test_stage_waits_while_any_request_is_outstanding(fresh_db, client):
    _request_demo_gap(client, "gap_01")
    _request_demo_gap(client, "gap_03")
    client.patch("/api/gaps/gap_01", json={"status": "dismissed"})
    assert _demo(client)["stage"] == "waiting_tenant"

    client.patch("/api/gaps/gap_03", json={"status": "dismissed"})
    assert _demo(client)["stage"] == "needs_review"


# --- Tenant submit ---------------------------------------------------------------------------


def test_submit_recomputes_stage_to_needs_review(fresh_db, client, anon_client, token):
    _request_demo_gap(client)
    _submit(anon_client, token)
    summary = _demo(client)
    assert summary["stage"] == "needs_review"
    assert "events to review" in summary["priority_reason"]
    assert summary["pending_review"] == 7
    assert summary["open_blocking"] == 2  # gap_01 still waiting_tenant, gap_02 open





def test_submit_with_unticked_box_is_422_and_keeps_stage(
    fresh_db, client, anon_client, token
):
    _request_demo_gap(client)
    r = anon_client.post(
        f"/api/intake/{token}/submit",
        json={"attest_genuine": True, "consent_share": False, "language": "zh"},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == ["Agree to share the files"]
    assert _demo(client)["stage"] == "waiting_tenant"


# --- Caseload order --------------------------------------------------------------------------


def test_caseload_is_sorted_while_waiting_and_after_submit(
    fresh_db, client, anon_client, token
):
    _assert_caseload_sorted(client)
    _request_demo_gap(client)
    cases = _assert_caseload_sorted(client)
    assert cases[0]["id"] == HEARING_CASE and cases[0]["priority"] == "high"

    _submit(anon_client, token)
    cases = _assert_caseload_sorted(client)
    assert cases[0]["id"] == HEARING_CASE


def _row_counts(session: Session) -> tuple[int, int]:
    """(open blocking gaps, pending events) straight from the rows, not the summary."""
    session.expire_all()
    gaps = session.exec(select(GapRow).where(GapRow.case_id == CASE)).all()
    events = session.exec(select(TimelineEventRow).where(TimelineEventRow.case_id == CASE)).all()
    open_states = ("open", "waiting_tenant")
    blocking = [g for g in gaps if g.severity == "blocking" and g.status in open_states]
    pending = [e for e in events if e.review_status == "pending"]
    return len(blocking), len(pending)


def test_demo_counts_match_gap_and_event_rows(fresh_db, client, session):
    summary = _demo(client)
    assert (summary["open_blocking"], summary["pending_review"]) == (2, 7)
    assert _row_counts(session) == (2, 7)

    client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    client.patch("/api/gaps/gap_02", json={"status": "dismissed"})
    summary = _demo(client)
    assert (summary["open_blocking"], summary["pending_review"]) == (1, 6)
    assert _row_counts(session) == (1, 6)


def test_stage_is_not_exposed_on_case_detail(fresh_db, client):
    assert "stage" not in client.get(f"/api/cases/{CASE}").json()


# --- Review progress drives needs_review <-> ready_to_file -----------------------------------


def test_resolving_gaps_and_reviewing_all_events_reaches_ready_to_file(fresh_db, client):
    assert _demo(client)["stage"] == "needs_review"
    client.patch("/api/gaps/gap_01", json={"status": "resolved", "value": "12 Harbour Rd"})
    client.patch(
        "/api/gaps/gap_02",
        json={"status": "resolved", "value": "Reduce rent for the affected period"},
    )
    assert _demo(client)["stage"] == "needs_review"  # events still pending

    pending = [
        e["id"] for e in client.get(f"/api/cases/{CASE}/timeline").json()
        if e["review_status"] == "pending"
    ]
    assert len(pending) == 7
    for i, event_id in enumerate(pending):
        status = "accepted" if i % 2 == 0 else "rejected"
        r = client.patch(f"/api/timeline/{event_id}", json={"review_status": status})
        assert r.status_code == 200
        if i < len(pending) - 1:
            assert _demo(client)["stage"] == "needs_review"

    summary = _demo(client)
    assert summary["stage"] == "ready_to_file"
    assert summary["priority_reason"] == "No open items"
    assert (summary["open_blocking"], summary["pending_review"]) == (0, 0)


def test_reopening_a_gap_moves_ready_to_file_back_to_needs_review(fresh_db, client):
    client.patch("/api/gaps/gap_01", json={"status": "resolved", "value": "12 Harbour Rd"})
    client.patch(
        "/api/gaps/gap_02",
        json={"status": "resolved", "value": "Reduce rent for the affected period"},
    )
    for event_id in ("te_02", "te_04", "te_05", "te_06", "te_07", "te_08", "te_09"):
        client.patch(f"/api/timeline/{event_id}", json={"review_status": "accepted"})
    assert _demo(client)["stage"] == "ready_to_file"

    client.patch("/api/gaps/gap_01", json={"status": "open"})
    summary = _demo(client)
    assert summary["stage"] == "needs_review"
    assert summary["open_blocking"] == 1


def test_dismissed_gaps_with_empty_required_fields_do_not_reach_ready_to_file(fresh_db, client):
    """Stage and package gating must agree: an empty required field blocks both."""
    client.patch("/api/gaps/gap_01", json={"status": "dismissed"})
    client.patch("/api/gaps/gap_02", json={"status": "dismissed"})
    for event_id in ("te_02", "te_04", "te_05", "te_06", "te_07", "te_08", "te_09"):
        client.patch(f"/api/timeline/{event_id}", json={"review_status": "accepted"})
    summary = _demo(client)
    assert (summary["open_blocking"], summary["pending_review"]) == (0, 0)
    assert summary["stage"] == "needs_review"


# --- hearing_scheduled is preserved ----------------------------------------------------------


def test_refresh_stage_never_overrides_hearing_scheduled(fresh_db, session):
    case = session.get(CaseRow, HEARING_CASE)
    assert case is not None and case.stage == "hearing_scheduled"
    refresh_stage(session, case)
    session.commit()
    session.refresh(case)
    assert case.stage == "hearing_scheduled"

    # Nothing open and a request outstanding would compute another stage; hearing wins.
    placeholder = session.get(GapRow, f"{HEARING_CASE}_gap_1")
    assert placeholder is not None
    placeholder.status = "resolved"
    session.add(placeholder)
    refresh_stage(session, case, waiting_on_tenant=True)
    session.commit()
    session.refresh(case)
    assert case.stage == "hearing_scheduled"


def test_mutation_on_hearing_case_keeps_hearing_scheduled(fresh_db, client):
    r = client.patch(f"/api/gaps/{HEARING_CASE}_gap_1", json={"status": "resolved"})
    assert r.status_code == 200
    summary = next(c for c in client.get("/api/cases").json() if c["id"] == HEARING_CASE)
    assert summary["stage"] == "hearing_scheduled"
    assert summary["open_blocking"] == 0
