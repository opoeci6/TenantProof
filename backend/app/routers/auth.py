from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.auth import authenticate, end_session, start_session
from app.config import Settings, get_settings
from app.deps import SessionDep
from app.models import OrgRow
from app.ratelimit import limiter, login_limit
from app.schemas import LoginRequest, Ok, Org, User

router = APIRouter()


@router.post("/auth/login", response_model=User)
@limiter.limit(login_limit)
def login(
    request: Request,
    body: LoginRequest,
    response: Response,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    user = authenticate(session, body.email, body.password)
    if user is None:
        # Same answer for unknown email and wrong password.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong email or password")
    start_session(session, user, settings, response)
    session.commit()
    org = session.get(OrgRow, user.org_id)
    assert org is not None
    return User(id=user.id, name=user.name, role="caseworker", org=Org(id=org.id, name=org.name))


@router.post("/auth/logout", response_model=Ok)
def logout(
    request: Request,
    response: Response,
    session: SessionDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Ok:
    end_session(session, request, settings, response)
    session.commit()
    return Ok()
