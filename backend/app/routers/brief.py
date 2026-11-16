from fastapi import APIRouter, HTTPException, Response, status

from app.deps import CaseDep, SessionDep
from app.services import case_brief

router = APIRouter()


@router.get("/cases/{case_id}/case-brief")
def get_case_brief(case: CaseDep, session: SessionDep) -> Response:
    """The case brief as a PDF, built on request from the case's rows. The case comes from the
    path and the caseworker's org (404 otherwise). 409 while no analysis has completed."""
    if reasons := case_brief.blockers(session, case):
        raise HTTPException(status.HTTP_409_CONFLICT, {"reasons": reasons})
    return Response(
        content=case_brief.build_brief(session, case),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'inline; filename="{case_brief.brief_filename(case)}"',
            "Cache-Control": "no-store",
        },
    )
