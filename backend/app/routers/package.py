import hashlib
import mimetypes
from collections.abc import Iterator
from pathlib import PurePosixPath
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Response, status
from fastapi.responses import StreamingResponse

from app.deps import CaseDep, SessionDep, UserDep
from app.schemas import Attestations, Package, PackageRequest
from app.services import package as svc
from app.services import storage

router = APIRouter()
_LANDLORD_PACKAGE_REASON = "Landlord cases use the manual Form J draft download."


def _refuse(code: int, reasons: list[str]) -> HTTPException:
    return HTTPException(code, {"reasons": reasons})


@router.post("/cases/{case_id}/package", response_model=Package)
def build_package(
    case: CaseDep,
    session: SessionDep,
    user: UserDep,
    body: Annotated[PackageRequest, Body(default_factory=PackageRequest)],
) -> Package:
    if case.applicant_role == "landlord":
        raise _refuse(status.HTTP_409_CONFLICT, [_LANDLORD_PACKAGE_REASON])
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


@router.get("/cases/{case_id}/package", response_model=Package)
def get_package_info(
    case: CaseDep,
    session: SessionDep,
    response: Response,
    version: int | None = Query(default=None, ge=1),
) -> Package:
    """#200: read a built package back. The page lost its contents and file links on reload."""
    if case.applicant_role == "landlord":
        raise _refuse(status.HTTP_409_CONFLICT, [_LANDLORD_PACKAGE_REASON])
    row = svc.get_package(session, case, version)
    response.headers["Cache-Control"] = "private, no-store"
    return svc.to_package(row)


@router.get("/cases/{case_id}/package/download")
def download_package(
    case: CaseDep, session: SessionDep, version: int | None = Query(default=None, ge=1)
) -> StreamingResponse:
    if case.applicant_role == "landlord":
        raise _refuse(status.HTTP_409_CONFLICT, [_LANDLORD_PACKAGE_REASON])
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


@router.get("/cases/{case_id}/package/files/{filename:path}")
def download_package_file(
    case: CaseDep,
    session: SessionDep,
    filename: str,
    version: int | None = Query(default=None, ge=1),
) -> Response:
    if case.applicant_role == "landlord":
        raise _refuse(status.HTTP_409_CONFLICT, [_LANDLORD_PACKAGE_REASON])
    row = svc.get_package(session, case, version)
    name, data = svc.package_file(row, filename)
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    leaf = PurePosixPath(name).name
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Length": str(len(data)),
            "Content-Disposition": f'attachment; filename="{leaf}"',
            "Cache-Control": "private, no-store",
            "X-File-Sha256": hashlib.sha256(data).hexdigest(),
        },
    )
