from collections.abc import Iterator
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.deps import CaseDep, SessionDep, UserDep
from app.schemas import Attestations, Package, PackageRequest
from app.services import package as svc
from app.services import storage

router = APIRouter()


def _refuse(code: int, reasons: list[str]) -> HTTPException:
    return HTTPException(code, {"reasons": reasons})


@router.post("/cases/{case_id}/package", response_model=Package)
def build_package(
    case: CaseDep,
    session: SessionDep,
    user: UserDep,
    body: Annotated[PackageRequest, Body(default_factory=PackageRequest)],
) -> Package:
    # Body first (422), then case state (409): CONTRACT_CHANGES #8.
    att = Attestations(**body.attestations.model_dump())
    if missing := svc.attestation_reasons(att):
        raise _refuse(status.HTTP_422_UNPROCESSABLE_CONTENT, missing)
    if blockers := svc.state_reasons(session, case):
        raise _refuse(status.HTTP_409_CONFLICT, blockers)
    row = svc.build_package(session, case, user, att)
    session.commit()
    session.refresh(row)
    return svc.to_package(row)


@router.get("/cases/{case_id}/package/download")
def download_package(
    case: CaseDep, session: SessionDep, version: int | None = Query(default=None, ge=1)
) -> StreamingResponse:
    row = svc.get_package(session, case, version)
    chunks: Iterator[bytes] = storage.open_chunks(row.storage_key)
    filename = f"tenantproof-{case.id}-v{row.version}.zip"
    return StreamingResponse(
        chunks, media_type="application/zip",
        headers={
            "Content-Length": str(row.size_bytes),
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Package-Sha256": row.sha256,
        },
    )
