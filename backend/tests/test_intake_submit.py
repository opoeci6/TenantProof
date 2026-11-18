"""Tenant intake submit: POST /api/intake/{token}/submit (BUILD_PLAN sections 6 and 7).
The tenant ticks two boxes; the server records attestation, logs it, and touches no evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app.models import CaseRow, EvidenceRow, IntakeTokenRow
from app.services.intake_token import hash_token

CASE = "case_demo_001"
VALID = {"attest_genuine": True, "consent_share": True, "language": "zh"}


def _submit(client, token: str, body: dict):
    return client.post(f"/api/intake/{token}/submit", json=body)


def _unattest(session: Session) -> None:
    """The demo seed is already attested; clear it so the submit actually changes something."""
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.tenant_attested_genuine = False
    case.tenant_attested_at = None
    session.add(case)
    session.commit()


def _intake(client) -> dict:
    return client.get(f"/api/cases/{CASE}").json()["intake"]


def _activity(client) -> list[dict]:
    return client.get(f"/api/cases/{CASE}/activity").json()


def _evidence_snapshot(session: Session) -> dict[str, dict]:
    session.expire_all()
    return {e.id: e.model_dump() for e in session.exec(select(EvidenceRow)).all()}


def test_submit_attests_the_case(fresh_db, token, client, anon_client, session):
    _unattest(session)
    before = _intake(client)
    assert before["tenant_attested_genuine"] is False
    assert before["tenant_attested_at"] is None

    r = _submit(anon_client, token, VALID)
    assert r.status_code == 200
    assert r.json() == {"ok": True}

    after = _intake(client)
    assert after["tenant_attested_genuine"] is True
    assert after["tenant_attested_at"] is not None





def test_submit_leaves_evidence_untouched(fresh_db, token, anon_client, session):
    before = _evidence_snapshot(session)
    assert len(before) > 0  # the demo case has seeded evidence
    _submit(anon_client, token, VALID)
    assert _evidence_snapshot(session) == before


def test_submit_logs_the_tenant_as_actor(fresh_db, token, client, anon_client):
    tenant_name = client.get(f"/api/cases/{CASE}").json()["tenant"]["name"]
    before = len(_activity(client))

    assert _submit(anon_client, token, VALID).status_code == 200

    acts = _activity(client)
    assert len(acts) == before + 1
    assert acts[0]["actor"] == "tenant"
    assert acts[0]["actor_name"] == tenant_name
    assert "genuine" in acts[0]["action"]


@pytest.mark.parametrize(
    ("body", "keywords"),
    [
        ({"attest_genuine": False, "consent_share": True}, ["genuine"]),
        ({"attest_genuine": True, "consent_share": False}, ["share"]),
        ({"attest_genuine": False, "consent_share": False}, ["genuine", "share"]),
    ],
)
def test_unticked_box_is_422_naming_the_box(
    fresh_db, token, client, anon_client, session, body, keywords
):
    _unattest(session)
    before = len(_activity(client))

    r = _submit(anon_client, token, body)

    assert r.status_code == 422
    detail = r.json()["detail"]
    assert set(detail) == {"reasons"}
    reasons = detail["reasons"]
    assert len(reasons) == len(keywords)
    for keyword in keywords:
        assert any(keyword in reason.lower() for reason in reasons), reasons
    # Refused means nothing changed: no attestation, no activity row.
    assert len(_activity(client)) == before
    assert _intake(client)["tenant_attested_genuine"] is False


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"attest_genuine": True},
        {"consent_share": True},
        {"attest_genuine": "not-a-bool", "consent_share": True},
    ],
)
def test_missing_or_invalid_body_fields_are_422(
    fresh_db, token, client, anon_client, session, body
):
    _unattest(session)
    before = len(_activity(client))

    assert _submit(anon_client, token, body).status_code == 422

    assert len(_activity(client)) == before
    assert _intake(client)["tenant_attested_genuine"] is False


def test_revoked_token_is_404(fresh_db, token, client, anon_client, session):
    old = token
    new = client.post(f"/api/cases/{CASE}/intake-link").json()["url"].rsplit("/", 1)[1]
    assert new != old
    _unattest(session)

    assert _submit(anon_client, old, VALID).status_code == 404
    assert _intake(client)["tenant_attested_genuine"] is False


def test_unknown_token_is_404(fresh_db, anon_client, client, session):
    _unattest(session)
    assert _submit(anon_client, "not-a-real-token", VALID).status_code == 404
    assert _intake(client)["tenant_attested_genuine"] is False


def test_expired_token_is_404(fresh_db, token, client, anon_client, session):
    row = session.get(IntakeTokenRow, hash_token(token))
    assert row is not None
    row.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.add(row)
    session.commit()
    _unattest(session)

    assert _submit(anon_client, token, VALID).status_code == 404
    assert _intake(client)["tenant_attested_genuine"] is False


def test_submit_is_rate_limited_to_twenty_per_minute(fresh_db, token, anon_client):
    for _ in range(20):
        assert _submit(anon_client, token, VALID).status_code == 200
    assert _submit(anon_client, token, VALID).status_code == 429
