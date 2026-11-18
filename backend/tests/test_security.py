"""Org isolation, intake-token hardening and internal-note leakage."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import select

from app.models import GapRow, IntakeTokenRow, TimelineEventRow
from app.services.intake_token import hash_token

CASE = "case_demo_001"


@pytest.mark.parametrize(
    "path",
    ["", "/evidence", "/timeline", "/gaps", "/sources", "/form-draft", "/activity",
     "/runs/latest"],
)
def test_other_org_gets_404_on_case_endpoints(other_org_client, path):
    assert other_org_client.get(f"/api/cases/{CASE}{path}").status_code == 404


def test_other_org_cannot_read_run_by_id(other_org_client):
    assert other_org_client.get("/api/runs/run_001").status_code == 404


def test_other_org_caseload_is_empty(other_org_client):
    assert other_org_client.get("/api/cases").json() == []


def test_no_cookie_is_401(anon_client):
    assert anon_client.get("/api/cases").status_code == 401
    assert anon_client.get("/api/me").status_code == 401


def test_unknown_token_is_404(client):
    assert client.get("/api/intake/not-a-token").status_code == 404


def test_old_token_returns_404_after_resend(client):
    first = client.post(f"/api/cases/{CASE}/intake-link").json()["url"].rsplit("/", 1)[1]
    assert client.get(f"/api/intake/{first}").status_code == 200
    second = client.post(f"/api/cases/{CASE}/intake-link").json()["url"].rsplit("/", 1)[1]
    assert second != first
    assert client.get(f"/api/intake/{first}").status_code == 404  # revoked
    assert client.get(f"/api/intake/{second}").status_code == 200


def test_token_is_random_and_only_its_hash_is_stored(client, session):
    token = client.post(f"/api/cases/{CASE}/intake-link").json()["url"].rsplit("/", 1)[1]
    assert len(token) >= 43  # token_urlsafe(32)
    row = session.get(IntakeTokenRow, hash_token(token))
    assert row is not None and not row.revoked
    assert token not in {row.token_hash, row.case_id}
    stored = " ".join(
        str(v) for r in session.exec(select(IntakeTokenRow)).all() for v in vars(r).values()
    )
    assert token not in stored
    case_json = client.get(f"/api/cases/{CASE}").text
    assert token not in case_json


def test_intake_token_expires(client, token, session):
    row = session.get(IntakeTokenRow, hash_token(token))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.add(row)
    session.commit()
    assert client.get(f"/api/intake/{token}").status_code == 404


def test_intake_link_post_logs_activity(client, session):
    before = len(client.get(f"/api/cases/{CASE}/activity").json())
    client.post(f"/api/cases/{CASE}/intake-link")
    acts = client.get(f"/api/cases/{CASE}/activity").json()
    assert len(acts) == before + 1
    assert acts[0]["actor"] == "caseworker" and "intake link" in acts[0]["action"]


def test_other_org_cannot_mint_link(other_org_client):
    assert other_org_client.post(f"/api/cases/{CASE}/intake-link").status_code == 404


def test_caseworker_note_never_in_form_or_tenant_view(client, token, session):
    note = "INTERNAL-ONLY-NOTE-XYZ"
    ev = session.get(TimelineEventRow, "te_03")
    assert ev is not None and ev.caseworker_note  # seed has one on an accepted event
    ev.caseworker_note = note
    session.add(ev)
    session.commit()
    form = client.get(f"/api/cases/{CASE}/form-draft").text
    tenant = client.get(f"/api/intake/{token}").text
    assert note not in form
    assert note not in tenant
    assert "caseworker_note" not in tenant


def test_tenant_view_only_lists_waiting_tenant_requests(client, token, session):
    assert client.get(f"/api/intake/{token}").json()["requests"] == []
    gap = session.exec(select(GapRow).where(GapRow.id == "gap_01")).one()
    gap.status = "waiting_tenant"
    session.add(gap)
    session.commit()
    try:
        reqs = client.get(f"/api/intake/{token}").json()["requests"]
        assert [r["id"] for r in reqs] == ["gap_01"]
        # The English copy goes to the tenant since #48 (CONTRACT_CHANGES #37); nothing more.
        assert set(reqs[0]) == {"id", "message", "message_en"}
    finally:
        gap.status = "open"
        session.add(gap)
        session.commit()
