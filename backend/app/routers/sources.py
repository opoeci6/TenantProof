from fastapi import APIRouter
from sqlmodel import col, select

from app.deps import CaseDep, SessionDep
from app.models import LegalSourceRow
from app.schemas import LegalSource
from app.services.mappers import to_source

router = APIRouter()


@router.get("/cases/{case_id}/sources", response_model=list[LegalSource])
def list_sources(case: CaseDep, session: SessionDep) -> list[LegalSource]:
    rows = session.exec(
        select(LegalSourceRow)
        .where(LegalSourceRow.case_id == case.id)
        .order_by(col(LegalSourceRow.position))
    ).all()
    return [to_source(r) for r in rows]
