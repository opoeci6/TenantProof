from fastapi import APIRouter

from app.deps import SessionDep, UserDep
from app.models import OrgRow
from app.schemas import Org, User

router = APIRouter()


@router.get("/me", response_model=User)
def get_me(session: SessionDep, user: UserDep) -> User:
    org = session.get(OrgRow, user.org_id)
    assert org is not None
    return User(id=user.id, name=user.name, role="caseworker", org=Org(id=org.id, name=org.name))
