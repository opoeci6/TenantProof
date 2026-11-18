"""Issue #95: the tenant types a short answer to a request.

The answer is stored as tenant-provided evidence (a text item, cited as "message 1"), recorded on
its request, and shown to the caseworker. The tenant's confirmation covers it like a file.
"""

from sqlmodel import Session, select

from app.models import CaseRow, EvidencePageRow, EvidenceRow, GapRow

CASE = "case_demo_001"
ANSWER = "房东叫陈先生，住在隔壁。"
VALID = {"attest_genuine": True, "consent_share": True, "language": "zh"}


def _submit(client, token: str, **extra):
    return client.post(f"/api/intake/{token}/submit", json={**VALID, **extra})


def _open_request(client, gap_id: str = "gap_01") -> dict:
    r = client.patch(f"/api/gaps/{gap_id}", json={"status": "waiting_tenant"})
    assert r.status_code == 200
    return r.json()


def _answer(gap_id: str = "gap_01", text: str = ANSWER) -> dict:
    return {"request_id": gap_id, "text": text}


def _unattest(session: Session) -> None:
    """The demo seed is already attested; clear it so a submit change is visible."""
    case = session.get(CaseRow, CASE)
    assert case is not None
    case.tenant_attested_genuine = False
    case.tenant_attested_at = None
    session.add(case)
    session.commit()


def test_an_answer_is_stored_as_tenant_evidence(fresh_db, anon_client, client, token, session):
    _open_request(client)
    r = _submit(anon_client, token, answers=[_answer()])
    assert r.status_code == 200 and r.json() == {"ok": True}

    session.expire_all()
    gap = session.get(GapRow, "gap_01")
    assert gap is not None and gap.answer is not None
    assert gap.answer["text"] == ANSWER
    assert gap.answer["received_at"] and gap.answer["evidence_id"]

    evidence = session.get(EvidenceRow, gap.answer["evidence_id"])
    assert evidence is not None
    assert evidence.uploaded_by == "tenant" and evidence.kind == "messages"
    pages = session.exec(
        select(EvidencePageRow).where(
            EvidencePageRow.evidence_id == evidence.id, EvidencePageRow.text == ANSWER
        )
    ).all()
    assert [p.locator for p in pages] == ["message 1"]


def test_the_answer_is_logged_and_the_case_is_attested(
    fresh_db, anon_client, client, token, session
) -> None:
    _open_request(client)
    _submit(anon_client, token, answers=[_answer()])
    session.expire_all()
    case = session.get(CaseRow, CASE)
    assert case is not None and case.tenant_attested_genuine is True
    gap = session.get(GapRow, "gap_01")
    assert gap is not None and gap.answer is not None
    evidence = session.get(EvidenceRow, gap.answer["evidence_id"])
    assert evidence is not None
    actions = [a.action for a in client.get(f"/api/cases/{CASE}/activity").json()]
    assert f"Uploaded {evidence.label} (answer.txt)" in actions
    assert "Confirmed the files are genuine and agreed to share them" in actions


def test_the_caseworker_sees_the_answer_on_the_request(fresh_db, anon_client, client, token):
    _open_request(client)
    _submit(anon_client, token, answers=[_answer()])
    listed = client.get(f"/api/cases/{CASE}/gaps").json()
    gap = next(g for g in listed if g["id"] == "gap_01")
    assert gap["answer"]["text"] == ANSWER


def test_an_answer_to_a_request_that_is_not_waiting_is_refused(
    fresh_db, anon_client, client, token, session
) -> None:
    r = _submit(anon_client, token, answers=[_answer()])
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == [
        "A request you answered is not open any more. Reload the page and try again"
    ]
    session.expire_all()
    gap = session.get(GapRow, "gap_01")
    assert gap is not None and gap.answer is None


def test_two_answers_for_the_same_request_are_refused(fresh_db, anon_client, client, token):
    _open_request(client)
    r = _submit(anon_client, token, answers=[_answer(), _answer(text="another answer")])
    assert r.status_code == 422
    assert r.json()["detail"]["reasons"] == ["Each request can have only one answer"]


def test_a_bad_answer_rolls_back_the_whole_submit(fresh_db, anon_client, client, token, session):
    _unattest(session)
    _open_request(client)
    r = _submit(
        anon_client,
        token,
        answers=[_answer(), {"request_id": "gap_99", "text": "no such request"}],
    )
    assert r.status_code == 422
    session.expire_all()
    case = session.get(CaseRow, CASE)
    assert case is not None and case.tenant_attested_genuine is False
    gap = session.get(GapRow, "gap_01")
    assert gap is not None and gap.answer is None


def test_more_than_ten_answers_are_refused(fresh_db, anon_client, client, token):
    _open_request(client)
    answers = [{"request_id": f"gap_{n:02d}", "text": ANSWER} for n in range(11)]
    r = _submit(anon_client, token, answers=answers)
    assert r.status_code == 422


def test_an_answer_longer_than_1000_characters_is_refused(fresh_db, anon_client, client, token):
    _open_request(client)
    r = _submit(anon_client, token, answers=[_answer(text="长" * 1001)])
    assert r.status_code == 422


def test_another_orgs_caseworker_cannot_see_the_answer(
    fresh_db, anon_client, other_org_client, client, token
) -> None:
    _open_request(client)
    _submit(anon_client, token, answers=[_answer()])
    assert other_org_client.get(f"/api/cases/{CASE}/gaps").status_code == 404
