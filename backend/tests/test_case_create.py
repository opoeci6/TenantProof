"""POST /cases (issue #41, CONTRACT_CHANGES #35): a caseworker creates a case in their own org.
Fixture providers only, so nothing reaches the network."""

from collections.abc import Iterator

import pytest
from sqlmodel import Session, select

from app.main import api
from app.models import ActivityRow, CaseRow, RunRow
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
