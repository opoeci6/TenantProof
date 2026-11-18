from fastapi import APIRouter
from sqlmodel import select

from app.deps import CaseDep, SessionDep, UserDep, load_owned
from app.models import CaseRow, TimelineEventRow
from app.schemas import TimelineEvent, TimelineEventPatch
from app.services import review
from app.services.mappers import to_event

router = APIRouter()


@router.get("/cases/{case_id}/timeline", response_model=list[TimelineEvent])
def list_timeline(case: CaseDep, session: SessionDep) -> list[TimelineEvent]:
    rows = session.exec(
        select(TimelineEventRow)
        .where(TimelineEventRow.case_id == case.id)
        .order_by(TimelineEventRow.sort_key)
    ).all()
    return [to_event(r) for r in rows]


@router.patch("/timeline/{event_id}", response_model=TimelineEvent)
def patch_event(
    event_id: str, body: TimelineEventPatch, session: SessionDep, user: UserDep
) -> TimelineEvent:
    event = load_owned(TimelineEventRow, event_id, session, user)
    case = session.get(CaseRow, event.case_id)
    assert case is not None
    review.patch_event(session, case, event, body, user)
    session.commit()
    session.refresh(event)
    return to_event(event)
