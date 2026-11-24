from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Response, status

from app.deps import CaseDep, SessionDep, UserDep
from app.schemas import FieldPatch, FormDraft, FormField
from app.services import review
from app.services.form_fields import covered_legacy_field_for_case
from app.services.mappers import build_form_draft, to_field
from app.services.package import application_pdf

router = APIRouter()


@router.get("/cases/{case_id}/form-draft", response_model=FormDraft)
def get_form_draft(case: CaseDep, session: SessionDep) -> FormDraft:
    return build_form_draft(session, case)


@router.get("/cases/{case_id}/form-draft/pdf")
def download_manual_form_draft(case: CaseDep, session: SessionDep, user: UserDep) -> Response:
    if case.applicant_role != "landlord":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Build the tenant evidence package to download the tenant's application draft.",
        )
    return Response(
        content=application_pdf(session, case, user.name, datetime.now(UTC)),
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="tenantproof-{case.id}-draft.pdf"',
            "Cache-Control": "no-store",
        },
    )


@router.patch("/cases/{case_id}/form-draft/fields/{key}", response_model=FormField)
def patch_field(
    key: str, body: FieldPatch, case: CaseDep, session: SessionDep, user: UserDep
) -> FormField:
    """CONTRACT_CHANGES #12: the case id is in the path because field keys repeat per case."""
    field = review.patch_field(session, case, key, body, user)
    session.commit()
    session.refresh(field)
    covered = {key} if covered_legacy_field_for_case(session, case.id, key) else set()
    return to_field(field, covered_fields=covered)
