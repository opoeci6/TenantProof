from fastapi import APIRouter

from app.deps import CaseDep, SessionDep, UserDep
from app.schemas import FieldPatch, FormDraft, FormField
from app.services import review
from app.services.mappers import build_form_draft, to_field

router = APIRouter()


@router.get("/cases/{case_id}/form-draft", response_model=FormDraft)
def get_form_draft(case: CaseDep, session: SessionDep) -> FormDraft:
    return build_form_draft(session, case)


@router.patch("/cases/{case_id}/form-draft/fields/{key}", response_model=FormField)
def patch_field(
    key: str, body: FieldPatch, case: CaseDep, session: SessionDep, user: UserDep
) -> FormField:
    """CONTRACT_CHANGES #12: the case id is in the path because field keys repeat per case."""
    field = review.patch_field(session, case, key, body, user)
    session.commit()
    session.refresh(field)
    return to_field(field)
