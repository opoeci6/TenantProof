"""Caseworker mutations (BUILD_PLAN sections 3, 6, 7): timeline review, gaps, tenant requests and
form fields. Every success logs exactly one caseworker activity entry; every by-id call is
org-scoped; rejected and pending events never reach the form narrative.
Stage and caseload behaviour lives in test_stage_transitions.py."""

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.models import TimelineEventRow

CASE = "case_demo_001"
FORM = f"/api/cases/{CASE}/form-draft"
NOTE = "INTERNAL-ONLY-NOTE-7731"


# Date order after te_02 is re-dated to 2026-09-01. te_07 is month precision ("2026-09"), which
# sorts before any day in September.
ORDER_AFTER_REDATE = [
    "te_01", "te_03", "te_04", "te_07", "te_02", "te_05", "te_06", "te_08", "te_09",
]

MUTATIONS = [
    pytest.param("PATCH", "/api/timeline/te_02", {"review_status": "accepted"},
                 id="event-accept"),
    pytest.param("PATCH", "/api/timeline/te_03", {"review_status": "rejected"},
                 id="event-reject"),
    pytest.param("PATCH", "/api/timeline/te_02", {"title": "Retitled"}, id="event-edit"),
    pytest.param("PATCH", "/api/timeline/te_02", {"caseworker_note": "internal"},
                 id="event-note"),
    pytest.param("PATCH", "/api/gaps/gap_01", {"status": "resolved", "value": "12 Harbour Rd"},
                 id="gap-resolve-value"),
    pytest.param("PATCH", "/api/gaps/gap_01", {"status": "dismissed"}, id="gap-dismiss"),
    pytest.param("POST", "/api/gaps/gap_01/tenant-request", None, id="gap-tenant-request"),
    pytest.param("PATCH", f"{FORM}/fields/landlord_address", {"value": "5 Elm St"},
                 id="field-set"),
]


def _timeline(client: TestClient) -> list[dict[str, Any]]:
    return client.get(f"/api/cases/{CASE}/timeline").json()


def _event(client: TestClient, event_id: str) -> dict[str, Any]:
    return next(e for e in _timeline(client) if e["id"] == event_id)


def _gaps(client: TestClient) -> list[dict[str, Any]]:
    return client.get(f"/api/cases/{CASE}/gaps").json()


def _gap(client: TestClient, gap_id: str) -> dict[str, Any]:
    return next(g for g in _gaps(client) if g["id"] == gap_id)


def _draft(client: TestClient) -> dict[str, Any]:
    return client.get(FORM).json()


def _field(client: TestClient, key: str) -> dict[str, Any]:
    return next(f for f in _draft(client)["fields"] if f["key"] == key)


def _activity(client: TestClient) -> list[dict[str, Any]]:
    return client.get(f"/api/cases/{CASE}/activity").json()


def _me_name(client: TestClient) -> str:
    return client.get("/api/me").json()["name"]


def _snapshot(client: TestClient) -> dict[str, Any]:
    """Everything a mutation could touch. Equal before and after means nothing changed."""
    return {
        "timeline": _timeline(client),
        "gaps": _gaps(client),
        "draft": _draft(client),
        "activity": _activity(client),
        "caseload": client.get("/api/cases").json(),
    }


# --- PATCH /api/timeline/{id} ----------------------------------------------------------


def test_accept_marks_event_reviewed_by_signed_in_user(fresh_db, client):
    r = client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    assert r.status_code == 200
    body = r.json()
    assert body["review_status"] == "accepted"
    assert body["reviewed_by"] == _me_name(client) == "Sam Rivera"


def test_accepting_adds_event_to_form_narrative(fresh_db, client):
    assert "te_02" not in _draft(client)["narrative"]["event_ids"]
    client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    narrative = _draft(client)["narrative"]
    assert "te_02" in narrative["event_ids"]
    assert _event(client, "te_02")["description"] in narrative["text"]


def test_rejecting_accepted_event_removes_it_from_narrative(fresh_db, client):
    description = _event(client, "te_03")["description"]
    before = _draft(client)["narrative"]
    assert "te_03" in before["event_ids"]
    assert description in before["text"]

    r = client.patch("/api/timeline/te_03", json={"review_status": "rejected"})
    assert r.status_code == 200 and r.json()["review_status"] == "rejected"

    after = _draft(client)["narrative"]
    assert "te_03" not in after["event_ids"]
    assert description not in after["text"]





def test_note_only_patch_keeps_rejected_event_rejected(fresh_db, client):
    client.patch("/api/timeline/te_04", json={"review_status": "rejected"})
    r = client.patch("/api/timeline/te_04", json={"caseworker_note": "called tenant"})
    assert r.status_code == 200
    assert r.json()["review_status"] == "rejected"
    assert "te_04" not in _draft(client)["narrative"]["event_ids"]


def test_resetting_accepted_event_to_pending_clears_reviewer_and_form(fresh_db, client):
    description = _event(client, "te_03")["description"]
    pending_before = len([e for e in _timeline(client) if e["review_status"] == "pending"])
    r = client.patch("/api/timeline/te_03", json={"review_status": "pending"})
    assert r.status_code == 200
    assert r.json()["review_status"] == "pending"
    assert r.json()["reviewed_by"] is None
    assert len([e for e in _timeline(client) if e["review_status"] == "pending"]) == (
        pending_before + 1
    )
    narrative = _draft(client)["narrative"]
    assert "te_03" not in narrative["event_ids"]
    assert description not in narrative["text"]


def test_resending_unchanged_date_keeps_its_basis(fresh_db, client):
    """The UI resends the original date on every edit. Only a real change becomes basis=user."""
    original = _event(client, "te_04")["date"]
    assert original["basis"] == "message_timestamp"
    r = client.patch(
        "/api/timeline/te_04",
        json={"title": "Landlord promises a visit (reworded)", "date": {**original}},
    )
    assert r.status_code == 200
    assert r.json()["date"] == original
    assert r.json()["review_status"] == "edited"


def _wipe_sources(session: Session, event_id: str) -> None:
    event = session.get(TimelineEventRow, event_id)
    assert event is not None
    event.sources = []
    session.add(event)
    session.commit()


@pytest.mark.parametrize(
    "body",
    [
        {"review_status": "accepted"},
        {"review_status": "edited"},
        {"description": "A reworded description"},  # implicit edit
    ],
    ids=["accept", "edit", "implicit-edit"],
)
def test_event_with_no_source_cannot_be_accepted_or_edited(fresh_db, client, session, body):
    _wipe_sources(session, "te_04")
    before = _snapshot(client)
    r = client.patch("/api/timeline/te_04", json=body)
    assert r.status_code == 422
    assert r.json()["detail"] == {"reasons": ["needs at least one source"]}
    assert _snapshot(client) == before  # nothing changed, nothing logged


def test_event_with_no_source_can_still_be_rejected_reset_or_annotated(
    fresh_db, client, session
):
    _wipe_sources(session, "te_04")
    for body, expected in (
        ({"review_status": "rejected"}, "rejected"),
        ({"caseworker_note": "no evidence for this yet"}, "rejected"),
        ({"review_status": "pending"}, "pending"),
    ):
        r = client.patch("/api/timeline/te_04", json=body)
        assert r.status_code == 200, r.text
        assert r.json()["review_status"] == expected
    assert "te_04" not in _draft(client)["narrative"]["event_ids"]


def test_edit_date_forces_user_basis_sets_edited_and_reorders(fresh_db, client):
    assert [e["id"] for e in _timeline(client)].index("te_02") == 1
    body = {
        "title": "Leak first photographed (date corrected)",
        "description": "Tenant photographed the leak on the bathroom ceiling.",
        "date": {"value": "2026-09-01", "precision": "day", "basis": "filename"},
    }
    r = client.patch("/api/timeline/te_02", json=body)
    assert r.status_code == 200
    event = r.json()
    assert event["review_status"] == "edited"
    assert event["date"] == {"value": "2026-09-01", "precision": "day", "basis": "user"}
    assert event["title"] == body["title"]
    assert event["reviewed_by"] == "Sam Rivera"

    assert [e["id"] for e in _timeline(client)] == ORDER_AFTER_REDATE

    narrative = _draft(client)["narrative"]
    assert "te_02" in narrative["event_ids"]
    assert f"Sep 1, 2026: {body['description']}" in narrative["text"]


def test_month_precision_event_renders_month_only_in_form(fresh_db, client):
    client.patch("/api/timeline/te_07", json={"review_status": "accepted"})
    description = _event(client, "te_07")["description"]
    assert _event(client, "te_07")["date"] == {
        "value": "2026-09", "precision": "month", "basis": "filename",
    }
    assert f"Sep 2026 (month only): {description}" in _draft(client)["narrative"]["text"]


def test_undated_event_sorts_last_and_renders_date_unknown(fresh_db, client):
    description = _event(client, "te_02")["description"]
    r = client.patch(
        "/api/timeline/te_02",
        json={"date": {"value": None, "precision": "unknown", "basis": "user"}},
    )
    assert r.status_code == 200
    assert r.json()["review_status"] == "edited"
    assert [e["id"] for e in _timeline(client)][-1] == "te_02"
    assert f"Date unknown: {description}" in _draft(client)["narrative"]["text"]


def test_caseworker_note_is_set_kept_and_cleared(fresh_db, client):
    r = client.patch("/api/timeline/te_02", json={"caseworker_note": "first"})
    assert r.json()["caseworker_note"] == "first"

    # Omitted note leaves it unchanged, even on a status change.
    r = client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    assert r.json()["caseworker_note"] == "first"

    # Explicit null clears it.
    r = client.patch("/api/timeline/te_02", json={"caseworker_note": None})
    assert r.json()["caseworker_note"] is None
    assert _event(client, "te_02")["caseworker_note"] is None


def test_whitespace_only_note_is_cleared(fresh_db, client):
    client.patch("/api/timeline/te_02", json={"caseworker_note": "first"})
    r = client.patch("/api/timeline/te_02", json={"caseworker_note": "   "})
    assert r.json()["caseworker_note"] is None


@pytest.mark.parametrize("body", [{"title": "   "}, {"description": ""}, {"title": None}])
def test_blank_title_or_description_is_422_and_changes_nothing(fresh_db, client, body):
    before = _snapshot(client)
    r = client.patch("/api/timeline/te_02", json=body)
    assert r.status_code == 422
    assert _snapshot(client) == before


def test_unknown_field_is_422_and_changes_nothing(fresh_db, client):
    before = _snapshot(client)
    r = client.patch("/api/timeline/te_02", json={"status": "accepted"})
    assert r.status_code == 422
    assert _snapshot(client) == before


def test_unknown_event_is_404(fresh_db, client):
    r = client.patch("/api/timeline/te_999", json={"review_status": "accepted"})
    assert r.status_code == 404


def test_caseworker_note_never_in_activity_form_or_tenant_view(fresh_db, client, token):
    r = client.patch("/api/timeline/te_03", json={"caseworker_note": NOTE})
    assert r.status_code == 200 and r.json()["caseworker_note"] == NOTE
    assert NOTE not in client.get(f"/api/cases/{CASE}/activity").text
    form = client.get(FORM).text
    assert NOTE not in form
    assert "caseworker_note" not in form
    assert NOTE not in client.get(f"/api/intake/{token}").text


# --- PATCH /api/gaps/{id} --------------------------------------------------------------


def test_resolving_with_value_fills_form_field_as_user_entered(fresh_db, client):
    r = client.patch(
        "/api/gaps/gap_01",
        json={"status": "resolved", "value": "12 Harbour Rd", "resolution_note": "x"},
    )
    assert r.status_code == 200
    gap = r.json()
    assert gap["status"] == "resolved" and gap["resolution_note"] == "x"
    field = _field(client, "landlord_address")
    assert field["value"] == "12 Harbour Rd"
    assert field["status"] == "user_entered"


def test_reopening_clears_field_and_resolution_note(fresh_db, client):
    client.patch(
        "/api/gaps/gap_01",
        json={"status": "resolved", "value": "12 Harbour Rd", "resolution_note": "x"},
    )
    r = client.patch("/api/gaps/gap_01", json={"status": "open"})
    assert r.status_code == 200
    assert r.json()["status"] == "open"
    assert r.json()["resolution_note"] is None
    field = _field(client, "landlord_address")
    assert field["value"] is None
    assert field["status"] == "missing"


def test_value_on_gap_without_form_field_is_422(fresh_db, client):
    before = _snapshot(client)
    r = client.patch("/api/gaps/gap_03", json={"status": "resolved", "value": "x"})
    assert r.status_code == 422
    assert _snapshot(client) == before


@pytest.mark.parametrize("status", ["open", "waiting_tenant", "dismissed"])
def test_value_requires_resolved_status(fresh_db, client, status):
    before = _snapshot(client)
    r = client.patch("/api/gaps/gap_01", json={"status": status, "value": "12 Harbour Rd"})
    assert r.status_code == 422
    assert _snapshot(client) == before


def test_blank_value_is_422_and_field_stays_missing(fresh_db, client):
    before = _snapshot(client)
    r = client.patch("/api/gaps/gap_01", json={"status": "resolved", "value": "   "})
    assert r.status_code == 422
    assert _snapshot(client) == before
    assert _field(client, "landlord_address")["status"] == "missing"


def test_dismissing_works_without_value_and_leaves_field_alone(fresh_db, client):
    r = client.patch("/api/gaps/gap_01", json={"status": "dismissed"})
    assert r.status_code == 200
    assert r.json()["status"] == "dismissed"
    assert r.json()["resolution_note"] is None
    assert _field(client, "landlord_address")["status"] == "missing"


def test_unknown_gap_is_404(fresh_db, client):
    assert client.patch("/api/gaps/gap_999", json={"status": "dismissed"}).status_code == 404


# --- POST /api/gaps/{id}/tenant-request ------------------------------------------------


def test_tenant_request_sets_waiting_and_tenant_sees_only_id_and_both_texts(
    fresh_db, client, token
):
    drafted = _gap(client, "gap_01")["tenant_request"]
    r = client.post("/api/gaps/gap_01/tenant-request")
    assert r.status_code == 200
    assert r.json()["status"] == "waiting_tenant"
    requests = client.get(f"/api/intake/{token}").json()["requests"]
    # The English goes to the tenant too since #48 (CONTRACT_CHANGES #37); nothing else does.
    assert requests == [
        {"id": "gap_01", "message": drafted["message"], "message_en": drafted["message_en"]}
    ]


def test_tenant_request_without_drafted_message_is_409(fresh_db, client):
    before = _snapshot(client)
    r = client.post("/api/gaps/gap_02/tenant-request")
    assert r.status_code == 409
    assert _snapshot(client) == before


@pytest.mark.parametrize("close_status", ["resolved", "dismissed"])
def test_tenant_request_on_closed_gap_is_409(fresh_db, client, close_status):
    client.patch("/api/gaps/gap_03", json={"status": close_status})
    before = _snapshot(client)
    r = client.post("/api/gaps/gap_03/tenant-request")
    assert r.status_code == 409
    assert _snapshot(client) == before
    assert _gap(client, "gap_03")["status"] == close_status


# --- PATCH /api/cases/{case}/form-draft/fields/{key} -----------------------------------


def test_setting_field_trims_value_and_resolves_open_gap(fresh_db, client):
    assert _gap(client, "gap_01")["status"] == "open"
    r = client.patch(f"{FORM}/fields/landlord_address", json={"value": " 5 Elm St "})
    assert r.status_code == 200
    assert r.json()["value"] == "5 Elm St"
    assert r.json()["status"] == "user_entered"
    assert _field(client, "landlord_address")["value"] == "5 Elm St"
    gap = _gap(client, "gap_01")
    assert gap["status"] == "resolved"
    assert "5 Elm St" in (gap["resolution_note"] or "")


def test_blank_field_value_sets_field_missing_and_null(fresh_db, client):
    client.patch(f"{FORM}/fields/landlord_address", json={"value": "5 Elm St"})
    r = client.patch(f"{FORM}/fields/landlord_address", json={"value": "   "})
    assert r.status_code == 200
    assert r.json()["value"] is None
    assert r.json()["status"] == "missing"
    field = _field(client, "landlord_address")
    assert field["value"] is None and field["status"] == "missing"


def test_emptying_a_field_reopens_the_gap_it_had_resolved(fresh_db, client):
    client.patch(f"{FORM}/fields/landlord_address", json={"value": "5 Elm St"})
    assert _gap(client, "gap_01")["status"] == "resolved"
    client.patch(f"{FORM}/fields/landlord_address", json={"value": ""})
    gap = _gap(client, "gap_01")
    assert gap["status"] == "open"
    assert gap["resolution_note"] is None


def test_null_field_value_is_422(fresh_db, client):
    before = _snapshot(client)
    r = client.patch(f"{FORM}/fields/landlord_address", json={"value": None})
    assert r.status_code == 422
    assert _snapshot(client) == before


def test_unknown_field_key_is_404(fresh_db, client):
    before = _snapshot(client)
    r = client.patch(f"{FORM}/fields/not_a_field", json={"value": "x"})
    assert r.status_code == 404
    assert _snapshot(client) == before


# --- Org ownership and sign-in, every mutation endpoint -------------------------------


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_other_org_gets_404_and_changes_nothing(
    fresh_db, client, other_org_client, method, path, body
):
    before = _snapshot(client)
    r = other_org_client.request(method, path, json=body)
    assert r.status_code == 404
    assert _snapshot(client) == before


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_signed_out_gets_401_and_changes_nothing(
    fresh_db, client, anon_client, method, path, body
):
    before = _snapshot(client)
    r = anon_client.request(method, path, json=body)
    assert r.status_code == 401
    assert _snapshot(client) == before


# --- Activity log: every success logs exactly one caseworker entry -------------------


@pytest.mark.parametrize(("method", "path", "body"), MUTATIONS)
def test_each_success_logs_exactly_one_caseworker_entry(fresh_db, client, method, path, body):
    before = len(_activity(client))
    r = client.request(method, path, json=body)
    assert r.status_code == 200
    acts = _activity(client)
    assert len(acts) == before + 1
    assert acts[0]["actor"] == "caseworker"
    assert acts[0]["actor_name"] == _me_name(client)
    assert acts[0]["action"]


@pytest.mark.parametrize("body", [{"status": "resolved"}, {"status": "dismissed"}])
def test_gap_status_change_logs_one_entry_that_names_the_item(fresh_db, client, body):
    before = len(_activity(client))
    client.patch("/api/gaps/gap_01", json=body)
    acts = _activity(client)
    assert len(acts) == before + 1
    assert _gap(client, "gap_01")["title"] in acts[0]["action"]


# --- Defects found while testing, now fixed ---------------------------------------------

NOOP_KINDS = ["same-title", "null-date", "same-date"]


def _noop_body(kind: str, event: dict[str, Any]) -> dict[str, Any]:
    """Bodies that change nothing. A no-op must not change the review status."""
    if kind == "same-title":
        return {"title": event["title"]}
    if kind == "null-date":
        return {"date": None}
    return {"date": event["date"]}


@pytest.mark.parametrize("kind", NOOP_KINDS)
def test_noop_patch_does_not_reapprove_rejected_event(fresh_db, client, kind):
    client.patch("/api/timeline/te_04", json={"review_status": "rejected"})
    r = client.patch("/api/timeline/te_04", json=_noop_body(kind, _event(client, "te_04")))
    assert r.status_code == 200
    assert r.json()["review_status"] == "rejected"
    assert "te_04" not in _draft(client)["narrative"]["event_ids"]


MALFORMED_DATES = ["not-a-date", "2026-09", "2026-13-45", "2026-02-30"]


@pytest.mark.parametrize("value", MALFORMED_DATES)
def test_malformed_date_is_rejected_with_422(fresh_db, client, value):
    before = _event(client, "te_02")["date"]
    r = client.patch(
        "/api/timeline/te_02", json={"date": {"value": value, "precision": "day", "basis": "user"}}
    )
    assert r.status_code == 422
    assert _event(client, "te_02")["date"] == before
