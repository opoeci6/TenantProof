from fastapi import APIRouter
from sqlmodel import col, select

from app.deps import CaseDep, SessionDep
from app.models import ActivityRow
from app.schemas import ActivityEntry
from app.services.mappers import to_activity

router = APIRouter()


@router.get("/cases/{case_id}/activity", response_model=list[ActivityEntry])
def list_activity(case: CaseDep, session: SessionDep) -> list[ActivityEntry]:
    rows = session.exec(
        select(ActivityRow)
        .where(ActivityRow.case_id == case.id)
        .order_by(col(ActivityRow.at).desc())
    ).all()
    return [to_activity(r) for r in rows]
