"""Seeding must never wipe a database that already has data (compose runs it on every start)."""

import pytest
from sqlalchemy import delete
from sqlmodel import Session, select

from app import seed as seed_module
from app.config import get_settings
from app.db import get_engine
from app.models import OrgRow, UserRow
from tests.conftest import CASEWORKER, _client

CASE = "case_demo_001"


def _accept(client) -> None:
    r = client.patch("/api/timeline/te_02", json={"review_status": "accepted"})
    assert r.status_code == 200


def _status(client) -> str:
    events = client.get(f"/api/cases/{CASE}/timeline").json()
    return next(e for e in events if e["id"] == "te_02")["review_status"]


def test_seed_skips_a_database_that_already_has_data(fresh_db, client, capsys):
    _accept(client)
    seed_module.main([])
    assert "not seeding" in capsys.readouterr().out
    assert _status(client) == "accepted"  # work survives a restart's seed call


def test_seed_force_wipes_and_reloads(fresh_db, client):
    _accept(client)
    seed_module.main(["--force"])
    assert _status(_client(CASEWORKER)) == "pending"  # new client: --force wipes sessions too


def test_seed_loads_an_empty_database(fresh_db, session):
    with Session(get_engine()) as s:
        for table in seed_module.CHILD_TABLES:
            s.exec(delete(table))  # type: ignore[call-overload]
        s.commit()
        assert seed_module.is_empty(s)
    seed_module.main([])
    session.expire_all()
    assert session.get(OrgRow, "org_harbourside") is not None


def test_seed_needs_a_password_only_when_it_will_seed(fresh_db, monkeypatch):
    monkeypatch.setattr(get_settings(), "seed_password", None)
    seed_module.main([])  # database has data: skipped, no password needed
    with pytest.raises(SystemExit):
        seed_module.main(["--force"])


# --- the BrAInstorm 2026 demo account -------------------------------------------------------------

DEMO_EMAIL = seed_module.DEMO_USER["email"]


def test_the_demo_account_is_in_the_demo_clinic_and_says_what_it_is_for(fresh_db, session):
    user = session.exec(select(UserRow).where(UserRow.email == DEMO_EMAIL)).one()
    assert user.org_id == "org_demo"
    assert "brainstorm2026" in user.email and "demo-only" in user.email


def test_the_demo_account_signs_in_with_the_seed_password_and_sees_the_demo_cases(fresh_db):
    from fastapi.testclient import TestClient

    from app.main import app

    c = TestClient(app, base_url="https://testserver")
    r = c.post("/api/auth/login",
               json={"email": DEMO_EMAIL, "password": get_settings().seed_password})
    assert r.status_code == 200
    ids = {case["id"] for case in c.get("/api/cases").json()}
    assert CASE in ids and LIVE in ids


def test_an_already_seeded_database_gets_the_demo_account_without_losing_work(
    fresh_db, client, session, capsys
):
    with Session(get_engine()) as s:
        s.exec(delete(UserRow).where(UserRow.email == DEMO_EMAIL))  # type: ignore[call-overload]
        s.commit()
    _accept(client)

    seed_module.main([])

    out = capsys.readouterr().out
    assert "not seeding" in out and f"added the demo account {DEMO_EMAIL}" in out
    assert session.exec(select(UserRow).where(UserRow.email == DEMO_EMAIL)).one()
    assert _status(client) == "accepted"
    seed_module.main([])  # a second start adds nothing and changes nothing
    assert "added the demo account" not in capsys.readouterr().out


def test_the_demo_account_is_not_added_without_a_password(fresh_db, session, monkeypatch):
    with Session(get_engine()) as s:
        s.exec(delete(UserRow).where(UserRow.email == DEMO_EMAIL))  # type: ignore[call-overload]
        s.commit()
    monkeypatch.setattr(get_settings(), "seed_password", None)
    seed_module.main([])
    session.expire_all()
    assert session.exec(select(UserRow).where(UserRow.email == DEMO_EMAIL)).first() is None


# --- case_live_001: the synthetic, empty case for the live demo run ----------------------------

LIVE = "case_live_001"


def _live_link(capsys) -> str:
    seed_module.main(["--force"])
    out = capsys.readouterr().out
    line = next(ln for ln in out.splitlines() if ln.startswith("live intake link"))
    return line.rsplit("/", 1)[1]


def test_seed_creates_the_live_case_with_nothing_in_it(fresh_db, session):
    from sqlmodel import func, select

    from app.models import (
        ActivityRow,
        CaseRow,
        EvidencePageRow,
        EvidenceRow,
        FormFieldRow,
        GapRow,
        IntakeTokenRow,
        LegalSourceRow,
        RunRow,
        TimelineEventRow,
    )

    session.expire_all()
    case = session.get(CaseRow, LIVE)
    demo = session.get(CaseRow, CASE)
    assert case is not None and demo is not None
    assert (case.tenant_name, case.tenant_language) == ("Mei Chen", "zh")
    assert case.title == "Water leak and mould on bathroom wall"  # matches the demo photo
    assert (case.org_id, case.assigned_to_id) == (demo.org_id, demo.assigned_to_id)
    assert case.synthetic is True and case.stage == "waiting_tenant"
    assert case.next_deadline is None and case.hearing_date is None

    for table in (EvidenceRow, EvidencePageRow, TimelineEventRow, GapRow, RunRow, FormFieldRow,
                  LegalSourceRow, ActivityRow):
        count = session.exec(
            select(func.count()).select_from(table).where(table.case_id == LIVE)  # type: ignore[attr-defined]
        ).one()
        assert count == 0, table.__name__

    tokens = session.exec(select(IntakeTokenRow).where(IntakeTokenRow.case_id == LIVE)).all()
    assert [t.revoked for t in tokens] == [False]  # its own single active token


def test_the_live_case_is_in_the_caseload_with_no_evidence(fresh_db, client, capsys):
    cases = {c["id"]: c for c in client.get("/api/cases").json()}
    assert cases[LIVE]["tenant_name"] == "Mei Chen" and cases[LIVE]["has_demo_data"] is True
    assert client.get(f"/api/cases/{LIVE}/evidence").json() == []
    assert client.get(f"/api/cases/{LIVE}/timeline").json() == []
    assert client.get(f"/api/cases/{LIVE}/runs/latest").status_code == 404


def test_the_live_case_has_its_own_intake_link_that_is_not_the_demo_cases(
    fresh_db, anon_client, capsys
):
    live = _live_link(capsys)
    view = anon_client.get(f"/api/intake/{live}")
    assert view.status_code == 200 and view.json()["tenant_first_name"] == "Mei"
    assert view.json()["language"] == "zh"


def test_the_demo_case_is_unchanged_by_the_live_case(fresh_db, client):
    assert len(client.get(f"/api/cases/{CASE}/evidence").json()) == 7
    assert len(client.get(f"/api/cases/{CASE}/timeline").json()) == 9


# --- seeded deadlines stay ahead of today and in one wording ------------------------------------


def test_the_seeded_caseload_never_shows_a_past_deadline_or_the_old_wording(fresh_db, client):
    from datetime import date

    rows = [c for c in client.get("/api/cases").json() if c["next_deadline"]]
    assert len(rows) == 2
    for row in rows:
        assert date.fromisoformat(row["next_deadline"]["date"]) >= date.today()
        assert row["next_deadline"]["label"].startswith("Must be received by · hearing ")
        assert not row["priority_reason"].startswith("Deadline passed")
    due = sorted(row["next_deadline"]["date"] for row in rows)
    assert due[0] < due[1]  # the seed's order is kept


def test_an_old_seed_still_reads_ahead_of_today_later(fresh_db, client, session, monkeypatch):
    """A database seeded days ago: the stored date is stale, the row it reads as is not."""
    from datetime import date, timedelta

    from app.models import CaseRow

    for case in session.exec(select(CaseRow)):
        if case.next_deadline:
            case.next_deadline = {
                **case.next_deadline, "label": case.next_deadline["label"].replace(
                    "Must be received by", "Evidence due"),
                "date": (date.today() - timedelta(days=3)).isoformat(),
            }
            session.add(case)
    session.commit()

    for row in client.get("/api/cases").json():
        if row["next_deadline"]:
            assert date.fromisoformat(row["next_deadline"]["date"]) >= date.today()
            assert row["next_deadline"]["label"].startswith("Must be received by")
