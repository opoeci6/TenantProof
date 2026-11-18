"""HTTP glue for the two upload routes: rate limit, body-size cap, rejections."""

import re

from fastapi import HTTPException, UploadFile
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.services.evidence.ingest import MAX_FILES, Upload, UploadRejected, check

UPLOAD_PATH = re.compile(r"/cases/[^/]+/evidence$|/intake/[^/]+/files$")
MULTIPART_SLACK = 1024 * 1024  # boundaries and part headers
TOO_LARGE = {"reasons": ["The upload is too large"]}


def upload_limit() -> str:
    return get_settings().upload_rate_limit


def body_limit() -> int:
    return MAX_FILES * get_settings().max_upload_bytes + MULTIPART_SLACK


def read_uploads(files: list[UploadFile]) -> list[Upload]:
    """Check every file (count, size, type) before anything is stored. Raises 413 or 415.
    The files stay spooled on disk; ingest reads one at a time."""
    try:
        return check([(f.filename, f.file) for f in files], get_settings().max_upload_bytes)
    except UploadRejected as exc:
        raise HTTPException(exc.status_code, {"reasons": exc.reasons}) from exc


class UploadBodyLimit:
    """FastAPI reads (and spools to disk) a multipart body before any dependency runs, so the
    auth, token, rate-limit and per-file checks all come after the body has arrived. This caps
    the body itself on the two upload routes: a declared Content-Length over the limit is
    refused before anything is read, and every body, chunked ones included, is counted as it
    arrives. At the limit the app is told the client left (so nothing more is read), and its
    answer to that is replaced with 413."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["method"] != "POST"
                or not UPLOAD_PATH.search(scope["path"])):
            await self.app(scope, receive, send)
            return
        limit = body_limit()
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await _too_large(scope, receive, send)
            return
        received, over, started = 0, False, False

        async def counted_receive() -> Message:
            nonlocal received, over
            if over:
                return {"type": "http.disconnect"}
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    over = True
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: Message) -> None:
            nonlocal started
            if over and not started:
                return  # the app's answer to the "disconnect" (a 400); 413 is sent below
            started = started or message["type"] == "http.response.start"
            await send(message)

        try:
            await self.app(scope, counted_receive, guarded_send)
        except Exception:
            if not over or started:
                raise
        if over and not started:
            await _too_large(scope, receive, send)


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    await JSONResponse({"detail": TOO_LARGE}, status_code=413)(scope, receive, send)
