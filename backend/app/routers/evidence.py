from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile, status
from sqlmodel import col, select

from app.deps import CaseDep, SessionDep, UserDep
from app.models import EvidenceRow
from app.ratelimit import limiter
from app.schemas import EvidenceItem
from app.services import storage
from app.services.evidence.http import read_uploads, upload_limit
from app.services.evidence.ingest import ingest
from app.services.mappers import to_evidence

router = APIRouter()

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# "no-cache": the browser must ask every time, so after sign-out a shared clinic PC shows no
# photo from its cache. The copy never changes (its storage key is its sha256), so that key is the
# ETag and an unchanged copy answers 304 without its bytes, after the login and org checks.
PREVIEW_CACHE = "private, no-cache"


@router.get("/cases/{case_id}/evidence", response_model=list[EvidenceItem])
def list_evidence(case: CaseDep, session: SessionDep) -> list[EvidenceItem]:
    rows = session.exec(
        select(EvidenceRow)
        .where(EvidenceRow.case_id == case.id)
        .order_by(col(EvidenceRow.position))
    ).all()
    return [to_evidence(r) for r in rows]


@router.post("/cases/{case_id}/evidence", response_model=list[EvidenceItem])
@limiter.limit(upload_limit)
def upload_evidence(
    request: Request, case: CaseDep, session: SessionDep, user: UserDep,
    file: list[UploadFile] = File(...),  # noqa: B008 (FastAPI's declaration style)
) -> list[EvidenceItem]:
    """Caseworker upload. Field `file`, repeatable (the UI sends every picked file at once)."""
    uploads = read_uploads(file)
    rows = ingest(session, case, uploads, "caseworker", user.name)
    session.commit()
    return [to_evidence(r) for r in rows]


@router.get("/cases/{case_id}/evidence/{evidence_id}/preview")
def evidence_preview(
    request: Request, case: CaseDep, evidence_id: str, session: SessionDep
) -> Response:
    """#97: a photo as the caseworker sees it. Only the copy made at upload with its EXIF and
    GPS removed (`stripped_sha256`, PNG or JPEG; a HEIC's copy is a JPEG), never the original.
    404 when the item is not in this case, has no such copy (not an image, or it could not be
    read) or the copy is not on disk. Reading it changes nothing, so nothing is logged."""
    row = session.get(EvidenceRow, evidence_id)
    key = row.stripped_sha256 if row is not None and row.case_id == case.id else None
    if key is None or not storage.exists(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No preview for this file")
    etag = f'"{key}"'
    headers = {"Cache-Control": PREVIEW_CACHE, "ETag": etag}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    data = storage.read(key)
    mime = "image/png" if data.startswith(PNG_SIGNATURE) else "image/jpeg"
    return Response(
        content=data, media_type=mime, headers={**headers, "Content-Disposition": "inline"}
    )
