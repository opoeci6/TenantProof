"""Reject an upload that can't be accepted before FastAPI parses (and spools) its multipart body.

FastAPI reads a multipart body before the route's dependencies run, so without this middleware
an upload with a bad credential could spool MAX_FILES x MAX_UPLOAD_BYTES (about 501 MB) to disk
before the route answered (issue #10). The middleware checks the credential first and answers
without touching the body:
- POST /api/intake/<token>/files: 404 for an unknown, revoked or expired intake link;
- POST /api/cases/<case_id>/evidence: 401 without a valid login session.
The routes still check everything again (the link, or the user's org and the case); this only
refuses early what they would refuse anyway. Rate limits run inside the routes, after the body
is read, so they cap stored uploads, not spooled bytes."""

import re

from sqlmodel import Session
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.auth import COOKIE_NAME, user_for_cookie
from app.db import get_engine
from app.models import CaseRow
from app.services.intake_token import find_active

FILES_PATH = re.compile(r"^/api/intake/(?P<token>[^/]+)/files$")
EVIDENCE_PATH = re.compile(r"^/api/cases/[^/]+/evidence$")
NOT_FOUND = {"detail": "Link not found"}
NOT_SIGNED_IN = {"detail": "Not signed in"}  # the same answer as auth.current_user


def token_is_active(token: str) -> bool:
    """The route's answer: unknown, revoked, expired or orphaned links are all 404."""
    with Session(get_engine()) as session:
        row = find_active(session, token)
        return row is not None and session.get(CaseRow, row.case_id) is not None


def signed_in(cookie: str | None) -> bool:
    """auth.current_user's answer for this session cookie value."""
    with Session(get_engine()) as session:
        return user_for_cookie(session, cookie) is not None


def _route_path(scope: Scope) -> str:
    """The path Starlette routes on: uvicorn puts root_path in front of `path`."""
    path, root = scope["path"], scope.get("root_path", "")
    return path[len(root):] if root and path.startswith(root) else path


def _session_cookie(scope: Scope) -> str | None:
    """The session cookie exactly as the route's `request.cookies` reads it, so a cookie the
    route would accept is never refused here. A Request built from the scope alone reads the
    headers only; it has no way to read the body."""
    return Request(scope).cookies.get(COOKIE_NAME)


class IntakeTokenGate:
    """ASGI middleware: an upload with a bad credential is answered before its body is read."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        path = _route_path(scope)
        match = FILES_PATH.match(path)
        if match is not None and not await run_in_threadpool(token_is_active,
                                                             match.group("token")):
            await JSONResponse(NOT_FOUND, status_code=404)(scope, receive, send)
            return
        if EVIDENCE_PATH.match(path) and not await run_in_threadpool(signed_in,
                                                                   _session_cookie(scope)):
            await JSONResponse(NOT_SIGNED_IN, status_code=401)(scope, receive, send)
            return
        await self.app(scope, receive, send)
