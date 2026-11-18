"""Request dependencies: current user and org-scoped loaders.

Every by-id handler loads objects through these helpers so a caller can never
read another org's data (returns 404, not 403, to avoid leaking existence)."""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlmodel import Session

from app.auth import current_user
from app.db import get_session
from app.models import CaseRow, UserRow

SessionDep = Annotated[Session, Depends(get_session)]
UserDep = Annotated[UserRow, Depends(current_user)]


def load_case(case_id: str, session: SessionDep, user: UserDep) -> CaseRow:
    case = session.get(CaseRow, case_id)
    if case is None or case.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Case not found")
    return case


CaseDep = Annotated[CaseRow, Depends(load_case)]


def load_owned(model: type, obj_id: str, session: Session, user: UserRow):
    """Load a case-child row by id and check its case belongs to the user's org."""
    obj = session.get(model, obj_id)
    case = session.get(CaseRow, obj.case_id) if obj is not None else None
    if obj is None or case is None or case.org_id != user.org_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    return obj
