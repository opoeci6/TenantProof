"""POST /cases (issue #41, CONTRACT_CHANGES #35): a caseworker creates a case in their own org.
Fixture providers only, so nothing reaches the network."""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlmodel import Session, select

from app.main import api
from app.models import (
    ActivityRow,
    CaseRow,
    FormFieldRow,
    GapRow,
    IntakeTokenRow,
    PackageRow,
    RunRow,
)
from app.routers import runs as runs_router
from app.services import cases as cases_service
from app.services.agent import llm
from tests.agent_support import tool_use, turn

URL = "/api/cases"
BODY = {
    "tenant_name": "Ana Silva",
    "tenant_language": "es",
    "unit": "Unit 2, 10 Sample Road, Halifax, NS",
    "title": "No hot water since October",
}
CHOOSE = "Preferred language: choose English, 中文, Français, Español, Русский or हिन्दी"


def create(client, **changes) -> dict:
    r = client.post(URL, json={**BODY, **changes})
    assert r.status_code == 200, r.text
    return r.json()


def test_the_case_lands_in_the_callers_org_assigned_to_them(fresh_db, client, session: Session):
    body = create(client)
    assert body["id"].startswith("case_") and len(body["id"]) == 15
    assert body["applicant_role"] == "tenant"
    assert body["tenant"] == {"name": "Ana Silva", "preferred_language": "es"}
    assert (body["title"], body["unit"]) == (BODY["title"], BODY["unit"])
    assert body["dispute_type"] == "maintenance" and body["synthetic"] is True
    assert body["assigned_to"]["name"] == "Sam Rivera"
    assert body["intake"]["status"] == "not_sent" and body["intake"]["url"] is None
    row = session.get(CaseRow, body["id"])
    me = session.get(CaseRow, "case_demo_001")
    assert row is not None and me is not None
    assert (row.org_id, row.assigned_to_id) == (me.org_id, me.assigned_to_id)
    assert row.stage == "waiting_tenant" and row.has_demo_data is True
    # in the caller's caseload, openable
    listed = {c["id"]: c for c in client.get(URL).json()}
    assert body["id"] in listed and listed[body["id"]]["has_demo_data"] is True
    assert listed[body["id"]]["applicant_role"] == "tenant"
    assert listed["case_demo_001"]["applicant_role"] == "tenant"


@pytest.mark.parametrize("role_body", [{}, {"applicant_role": "tenant"}])
def test_new_tenant_case_leaves_core_fields_for_analysis(fresh_db, client, session, role_body):
    created = create(client, **role_body)
    case_id = created["id"]
    assert created["applicant_role"] == "tenant"
    assert client.get(f"{URL}/{case_id}/form-draft").json()["fields"] == []
    assert session.exec(select(FormFieldRow).where(FormFieldRow.case_id == case_id)).first() is None
    assert client.post(f"{URL}/{case_id}/intake-link").status_code == 200


def test_landlord_case_gets_a_scoped_manual_draft(fresh_db, client, session):
    created = create(client, applicant_role="landlord")
    base = f"{URL}/{created['id']}"
    assert created["applicant_role"] == "landlord"
    assert client.get(base).json()["applicant_role"] == "landlord"
    listed = {item["id"]: item for item in client.get(URL).json()}
    assert listed[created["id"]]["applicant_role"] == "landlord"
    row = session.get(CaseRow, created["id"])
    assert row is not None and row.applicant_role == "landlord"
    draft = client.get(f"{base}/form-draft").json()
    fields = {field["key"]: field for field in draft["fields"]}
    assert fields["tenant_name"]["value"] == BODY["tenant_name"]
    assert fields["rental_address"]["value"] == BODY["unit"]
    assert {"tenant_email", "landlord_email", "move_in_date", "written_lease"} <= fields.keys()
    assert {"landlord_request_unpaid_rent", "landlord_request_unpaid_rent_reason"} <= fields.keys()
    assert {"remedy", "request_repairs", "request_repairs_reason"}.isdisjoint(fields)
    key = "landlord_request_unpaid_rent"
    r = client.patch(f"{base}/form-draft/fields/{key}", json={"value": "yes"})
    assert r.is_success, r.text


def test_landlord_intake_link_refusal_leaves_case_unchanged(fresh_db, client, session):
    case_id = create(client, applicant_role="landlord")["id"]
    response = client.post(f"{URL}/{case_id}/intake-link")
    assert response.status_code == 409, response.text
    row = session.get(CaseRow, case_id)
    assert row is not None and row.intake_status == "not_sent" and row.intake_sent_at is None
    tokens = select(IntakeTokenRow).where(IntakeTokenRow.case_id == case_id)
    assert session.exec(tokens).first() is None
    assert cases_service_log(session, case_id) == "Created the case"


@pytest.mark.parametrize(("role", "excluded"), [
    ("tenant", "landlord_request_unpaid_rent"),
    ("landlord", "request_repairs"),
])
def test_form_and_gap_updates_reject_another_roles_field(
    fresh_db, client, session, role, excluded,
):
    case_id = create(client, applicant_role=role)["id"]
    for key in (excluded, f"{excluded}_reason"):
        r = client.patch(f"{URL}/{case_id}/form-draft/fields/{key}", json={"value": "yes"})
        assert r.status_code == 404, r.text
        assert session.get(FormFieldRow, (case_id, key)) is None

    gap = GapRow(
        id="gap_outside_applicant_role", case_id=case_id, severity="blocking",
        title="Review the request", why="The draft needs review", how_to_fix="Confirm the request",
        input={"kind": "field", "field_key": excluded},
    )
    session.add(gap)
    session.commit()
    response = client.patch(f"/api/gaps/{gap.id}", json={"status": "dismissed"})
    assert response.status_code == 404, response.text
    session.refresh(gap)
    assert gap.status == "open"


def test_the_form_name_and_disclaimer_are_the_seeded_cases(fresh_db, client, session: Session):
    """Copied, not retyped: a new case shows the same form header as the live-demo case."""
    body = create(client)
    row, live = session.get(CaseRow, body["id"]), session.get(CaseRow, "case_live_001")
    assert row is not None and live is not None
    assert (row.form_name, row.form_disclaimer) == (live.form_name, live.form_disclaimer)
    assert row.form_name == cases_service.FORM_NAME


def test_another_org_cannot_see_it(fresh_db, client, other_org_client):
    body = create(client)
    assert body["id"] not in {c["id"] for c in other_org_client.get(URL).json()}
    for path in ("", "/evidence", "/timeline", "/gaps", "/form-draft"):
        r = other_org_client.get(f"{URL}/{body['id']}{path}")
        assert r.status_code == 404, path
    unknown = other_org_client.get(f"{URL}/case_doesnotexist")
    assert unknown.json() == other_org_client.get(f"{URL}/{body['id']}").json()


def test_the_other_org_creates_in_its_own_org(fresh_db, client, other_org_client):
    body = create(other_org_client)
    assert body["id"] not in {c["id"] for c in client.get(URL).json()}
    assert body["id"] in {c["id"] for c in other_org_client.get(URL).json()}


def test_the_body_cannot_choose_the_org_or_the_stage(fresh_db, client, session: Session):
    r = client.post(URL, json={**BODY, "org_id": "org_harbourside", "stage": "ready_to_file"})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == [
        "org_id: not a field of a new case", "stage: not a field of a new case"
    ]
    assert session.exec(select(CaseRow).where(CaseRow.title == BODY["title"])).first() is None


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"applicant_role": "other"}, "Applicant role: not valid"),
        ({"tenant_language": "ar"}, CHOOSE),
        ({"tenant_language": "de"}, CHOOSE),
        ({"tenant_name": ""}, "Tenant name: enter some text"),
        ({"tenant_name": "   \t "}, "Tenant name: enter some text"),
        ({"unit": "\x00\x01"}, "Unit address: enter some text"),
        ({"title": "x" * 121}, "What is wrong: at most 120 characters"),
        ({"unit": "x" * 201}, "Unit address: at most 200 characters"),
        ({"tenant_name": 42}, "Tenant name: not valid"),
    ],
)
def test_a_bad_body_is_422_with_plain_reasons(fresh_db, client, session, changes, reason):
    before = len(session.exec(select(CaseRow)).all())
    r = client.post(URL, json={**BODY, **changes})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == [reason]
    session.expire_all()
    assert len(session.exec(select(CaseRow)).all()) == before


def test_a_missing_field_and_a_missing_body_are_422(fresh_db, client):
    partial = {k: v for k, v in BODY.items() if k != "title"}
    r = client.post(URL, json=partial)
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == ["What is wrong: enter some text"]
    r = client.post(URL)
    assert r.status_code == 422 and len(r.json()["detail"]["reasons"]) == 1
    r = client.post(URL, json=["not", "an", "object"])
    assert r.status_code == 422


def test_signed_out_is_401_and_creates_nothing(fresh_db, anon_client, session: Session):
    assert anon_client.post(URL, json=BODY).status_code == 401
    assert session.exec(select(CaseRow).where(CaseRow.title == BODY["title"])).first() is None


def test_the_activity_log_says_who_created_it(fresh_db, client, session: Session):
    body = create(client)
    rows = session.exec(select(ActivityRow).where(ActivityRow.case_id == body["id"])).all()
    assert [(a.actor, a.actor_name, a.action) for a in rows] == [
        ("caseworker", "Sam Rivera", "Created the case")
    ]
    feed = client.get(f"{URL}/{body['id']}/activity").json()
    assert feed[0]["action"] == "Created the case"


def test_control_and_invisible_characters_are_removed(fresh_db, client):
    """The title and name reach the agent's prompts later: no control characters, and no
    zero-width or bidi characters that hide or reverse text."""
    body = create(
        client, title="Leak\x00 in the​ bath﻿room", tenant_name="Ana\x07 ‮Silva"
    )
    assert body["title"] == "Leak  in the bathroom"  # the NUL became a space
    assert body["tenant"]["name"] == "Ana  Silva"


@pytest.mark.parametrize("angle", ["<", "＜", "﹤"])
def test_a_less_than_sign_is_refused_not_stored_escaped(fresh_db, client, angle):
    """Escaped, "Heat < 15C" would show as "Heat &lt; 15C" on every page and in the filed PDF."""
    r = client.post(URL, json={**BODY, "title": f"Heat {angle} 15C", "unit": "<script>"})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == [
        "Unit address: remove the < character", "What is wrong: remove the < character"
    ]


@pytest.mark.parametrize("name", ["​", "⁠﻿", "‮", "\x00​ "])
def test_a_value_with_nothing_visible_is_blank(fresh_db, client, name):
    r = client.post(URL, json={**BODY, "tenant_name": name})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == ["Tenant name: enter some text"]


def test_the_caseload_says_the_link_is_not_sent_yet(fresh_db, client):
    body = create(client)
    row = next(c for c in client.get(URL).json() if c["id"] == body["id"])
    assert row["priority_reason"] == "Intake link not sent yet."
    assert client.post(f"{URL}/{body['id']}/intake-link").status_code == 200
    row = next(c for c in client.get(URL).json() if c["id"] == body["id"])
    assert row["priority_reason"] == "Intake link sent. No files from the tenant yet."


def test_every_page_of_an_empty_case_answers(fresh_db, client):
    body = create(client)
    base = f"{URL}/{body['id']}"
    for path in ("", "/evidence", "/timeline", "/gaps", "/sources", "/activity", "/form-draft"):
        r = client.get(base + path)
        assert r.status_code == 200, (path, r.text)
    assert client.get(base + "/evidence").json() == []
    assert client.get(base + "/form-draft").json()["fields"] == []
    assert client.get(base + "/runs/latest").status_code == 404  # API_CONTRACT: 404 if none


def test_a_retried_create_returns_the_first_case_and_logs_it_once(fresh_db, client, session):
    """#202: the retry after a lost response opens the case the first attempt made."""
    first = create(client, idempotency_key="retry-1")
    again = create(client, idempotency_key="retry-1")
    assert again["id"] == first["id"]
    rows = session.exec(select(CaseRow).where(CaseRow.idempotency_key == "retry-1")).all()
    assert [row.id for row in rows] == [first["id"]]
    assert cases_service_log(session, first["id"]) == "Created the case"
    assert len([
        c for c in client.get(URL).json() if c["tenant_name"] == BODY["tenant_name"]
    ]) == 1


def test_a_retried_landlord_create_does_not_duplicate_its_form_fields(fresh_db, client, session):
    first = create(client, applicant_role="landlord", idempotency_key="retry-landlord")
    again = create(client, applicant_role="landlord", idempotency_key="retry-landlord")
    assert again["id"] == first["id"]
    other = create(client, applicant_role="landlord", idempotency_key="retry-landlord-2")
    counted = {
        case_id: len(session.exec(
            select(FormFieldRow).where(FormFieldRow.case_id == case_id)
        ).all())
        for case_id in (first["id"], other["id"])
    }
    assert counted[first["id"]] == counted[other["id"]] > 0
    assert cases_service_log(session, first["id"]) == "Created the case"


def test_two_deliberate_submissions_are_two_cases(fresh_db, client, session):
    first = create(client, idempotency_key="one")
    second = create(client, idempotency_key="two")
    third = create(client)  # no key at all, as a caller that does not send one
    assert len({first["id"], second["id"], third["id"]}) == 3
    assert len(session.exec(select(CaseRow).where(CaseRow.id == third["id"])).all()) == 1


def test_another_org_using_the_same_key_creates_its_own_case(
    fresh_db, client, other_org_client, session: Session,
):
    """Keys are scoped per org: the same string in another org is a different submission."""
    mine = create(client, idempotency_key="shared-key")
    theirs = create(other_org_client, idempotency_key="shared-key")
    assert theirs["id"] != mine["id"]
    rows = session.exec(select(CaseRow).where(CaseRow.idempotency_key == "shared-key")).all()
    assert len({row.org_id for row in rows}) == 2


@pytest.mark.parametrize(("key", "reason"), [
    ("", "Submission key: enter some text"),
    ("   ", "Submission key: enter some text"),
    ("x" * 65, "Submission key: at most 64 characters"),
    ("<bad>", "Submission key: not valid"),
])
def test_a_bad_submission_key_is_422_and_binds_nothing(fresh_db, client, session, key, reason):
    before = len(session.exec(select(CaseRow)).all())
    r = client.post(URL, json={**BODY, "idempotency_key": key})
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == [reason]
    session.expire_all()
    assert len(session.exec(select(CaseRow)).all()) == before


def test_a_refused_body_does_not_bind_its_key(fresh_db, client, session):
    """The 422 comes before the lookup or the write, so the key is free for the corrected retry."""
    refused = client.post(
        URL, json={**BODY, "title": "Heat < 15C", "idempotency_key": "after-refusal"}
    )
    assert refused.status_code == 422
    assert session.exec(
        select(CaseRow).where(CaseRow.idempotency_key == "after-refusal")
    ).first() is None
    made = create(client, idempotency_key="after-refusal")
    rows = session.exec(select(CaseRow).where(CaseRow.idempotency_key == "after-refusal")).all()
    assert [row.id for row in rows] == [made["id"]]


def test_a_key_race_returns_the_case_that_won(fresh_db, client, session, monkeypatch):
    """Two requests with one key: the index refuses the second insert and the handler answers
    with the case the winner committed, not a 500."""
    first = create(client, idempotency_key="race-key")
    real = cases_service.find_by_idempotency_key
    calls = {"n": 0}

    def racing(session_: Session, org_id: str, key: str | None):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(session_, org_id, key)

    monkeypatch.setattr(cases_service, "find_by_idempotency_key", racing)
    again = client.post(URL, json={**BODY, "idempotency_key": "race-key"})
    assert again.status_code == 200, again.text
    assert again.json()["id"] == first["id"]
    rows = session.exec(select(CaseRow).where(CaseRow.idempotency_key == "race-key")).all()
    assert [row.id for row in rows] == [first["id"]]
    assert cases_service_log(session, first["id"]) == "Created the case"


def test_the_case_says_whether_it_has_a_run_and_a_package(fresh_db, client, session: Session):
    """#218: the page reads these flags instead of asking for a run and a package it has not got.
    The two endpoints keep their 404 answers for a caller that does ask."""
    body = create(client)
    base = f"{URL}/{body['id']}"
    assert (body["has_run"], body["has_package"]) == (False, False)
    assert client.get(f"{base}/runs/latest").status_code == 404
    assert client.get(f"{base}/package").status_code == 404
    session.add(RunRow(
        id="run_flag", case_id=body["id"], status="running", started_at=datetime.now(UTC),
    ))
    session.add(PackageRow(
        id="pkg_flag", case_id=body["id"], version=1, sha256="0" * 64, storage_key="blob",
        size_bytes=1, prepared_by="Sam Rivera", attestations={},
    ))
    session.commit()
    flags = client.get(base).json()
    assert (flags["has_run"], flags["has_package"]) == (True, True)
    assert client.get(f"{base}/runs/latest").status_code == 200
    assert client.get(f"{base}/package").status_code == 200


def test_the_intake_link_works_on_a_new_case(fresh_db, client):
    body = create(client)
    r = client.post(f"{URL}/{body['id']}/intake-link")
    assert r.status_code == 200 and r.json()["status"] == "sent"
    token = r.json()["url"].rsplit("/", 1)[1]
    view = client.get(f"/api/intake/{token}")
    assert view.status_code == 200
    assert view.json()["language"] == "es" and view.json()["unit"] == BODY["unit"]


@pytest.fixture
def provider() -> Iterator[None]:
    """A new case has no evidence: extract asks nothing, the timeline agent finishes at once,
    and the runner then asks for the hearing date."""
    script = {"timeline-turn1": turn(tool_use("finish", "t1", summary="No evidence yet."))}
    api.dependency_overrides[runs_router.provider_dep] = (
        lambda: llm.FixtureProvider(scripted=script)
    )
    yield
    api.dependency_overrides.pop(runs_router.provider_dep, None)


def test_a_run_starts_on_a_new_case(fresh_db, client, session: Session, provider):
    body = create(client)
    r = client.post(f"{URL}/{body['id']}/runs")
    assert r.status_code == 200 and r.json()["status"] == "running"
    session.expire_all()
    run = session.get(RunRow, r.json()["id"])
    assert run is not None and run.status != "failed"


@pytest.mark.parametrize(("code", "name"), [("ru", "Russian (Русский)"), ("hi", "Hindi (हिन्दी)")])
def test_a_case_can_be_created_in_russian_and_hindi(fresh_db, client, session, code, name):
    """#68, #69: the language is stored, the tenant page opens in it, and the log names it."""
    body = create(client, tenant_language=code)
    assert body["tenant"]["preferred_language"] == code
    link = client.post(f"{URL}/{body['id']}/intake-link").json()["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/intake/{link}").json()["language"] == code
    assert name in cases_service_log(session, body["id"])


def cases_service_log(session: Session, case_id: str) -> str:
    rows = session.exec(select(ActivityRow).where(ActivityRow.case_id == case_id)).all()
    return " | ".join(r.action for r in rows)
