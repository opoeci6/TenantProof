from io import BytesIO

from fastapi import APIRouter, File, HTTPException, Request, UploadFile, status
from sqlmodel import Session, col, select

from app.deps import SessionDep
from app.models import CaseRow, GapRow, OrgRow
from app.ratelimit import limiter, submit_limit
from app.schemas import IntakeAnswer, IntakeFilesReceived, IntakeSubmit, IntakeView, Ok
from app.services import review
from app.services.evidence import sniff
from app.services.evidence.http import read_uploads, upload_limit
from app.services.evidence.ingest import Upload, ingest
from app.services.intake_token import find_active
from app.services.mappers import iso_req, to_intake_view

router = APIRouter()


def _not_found() -> HTTPException:
    # Same answer for unknown, revoked and expired links: nothing to probe.
    return HTTPException(status.HTTP_404_NOT_FOUND, "Link not found")


def _refuse(*reasons: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": list(reasons)})


def _record_answers(
    session: Session, case: CaseRow, answers: list[IntakeAnswer], actor_name: str
) -> None:
    """Store each typed answer as a TXT evidence item and record it on its request (issue #95).
    Every id is checked before anything is written, so a refused answer saves nothing."""
    if not answers:
        return
    ids = [answer.request_id for answer in answers]
    if len(set(ids)) != len(ids):
        raise _refuse("Each request can have only one answer")
    gaps = {
        gap.id: gap
        for gap in session.exec(
            select(GapRow).where(GapRow.case_id == case.id, col(GapRow.id).in_(ids))
        ).all()
    }
    stale = [
        gap_id
        for gap_id in ids
        if gaps.get(gap_id) is None
        or gaps[gap_id].status != "waiting_tenant"
        or gaps[gap_id].tenant_request is None
    ]
    if stale:
        raise _refuse(
            "A request you answered is not open any more. Reload the page and try again"
        )
    uploads = [
        Upload(
            filename="answer.txt",
            mime=sniff.TEXT,
            size=len(answer.text.encode("utf-8")),
            source=BytesIO(answer.text.encode("utf-8")),
        )
        for answer in answers
    ]
    rows = ingest(session, case, uploads, "tenant", actor_name)
    for answer, row in zip(answers, rows, strict=True):
        gap = gaps[answer.request_id]
        gap.answer = {
            "text": answer.text,
            "received_at": iso_req(row.received_at),
            "evidence_id": row.id,
        }
        session.add(gap)


@router.get("/intake/{token}", response_model=IntakeView)
def get_intake(token: str, session: SessionDep) -> IntakeView:
    row = find_active(session, token)
    case = session.get(CaseRow, row.case_id) if row else None
    if case is None:
        raise _not_found()
    org = session.get(OrgRow, case.org_id)
    assert org is not None
    return to_intake_view(session, case, org.name)


@router.post("/intake/{token}/submit", response_model=Ok)
@limiter.limit(submit_limit)
def submit_intake(request: Request, token: str, body: IntakeSubmit, session: SessionDep) -> Ok:
    row = find_active(session, token)
    case = session.get(CaseRow, row.case_id) if row else None
    if case is None:
        raise _not_found()
    missing = [
        text
        for ok, text in (
            (body.attest_genuine, "Confirm the files are genuine"),
            (body.consent_share, "Agree to share the files"),
        )
        if not ok
    ]
    if missing:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": missing})
    _record_answers(session, case, body.answers, case.tenant_name)
    review.record_tenant_submit(session, case)
    session.commit()
    return Ok()


@router.post("/intake/{token}/files", response_model=IntakeFilesReceived)
@limiter.limit(upload_limit)
def upload_intake_files(
    request: Request, token: str, session: SessionDep,
    file: list[UploadFile] = File(...),  # noqa: B008 (FastAPI's declaration style)
) -> IntakeFilesReceived:
    """Tenant upload through the intake link. Field `file`, repeatable."""
    row = find_active(session, token)
    case = session.get(CaseRow, row.case_id) if row else None
    if case is None:
        raise _not_found()
    uploads = read_uploads(file)
    rows = ingest(session, case, uploads, "tenant", case.tenant_name)
    session.commit()
    return IntakeFilesReceived(received=len(rows))
