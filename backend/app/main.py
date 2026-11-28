import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlmodel import Session
from starlette.concurrency import run_in_threadpool
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.access_log import MaskIntakeTokens
from app.auth import purge_expired_sessions
from app.config import get_settings
from app.db import get_engine
from app.ratelimit import limiter
from app.routers import (
    activity,
    auth,
    brief,
    cases,
    evidence,
    form,
    gaps,
    intake,
    me,
    package,
    privacy,
    runs,
    sources,
    timeline,
    translation,
)
from app.services import health
from app.services.evidence.http import UploadBodyLimit
from app.services.intake_gate import IntakeTokenGate

log = logging.getLogger(__name__)
_access_log = logging.getLogger("uvicorn.access")
if not any(isinstance(f, MaskIntakeTokens) for f in _access_log.filters):
    _access_log.addFilter(MaskIntakeTokens())
PURGE_EVERY_S = 3600.0  # #59: how often expired login sessions are cleared out


def _purge_once() -> int:
    with Session(get_engine()) as session:
        removed = purge_expired_sessions(session)
        session.commit()
        return removed


async def _purge_forever() -> None:
    """Clear out expired login sessions now and then every PURGE_EVERY_S, until cancelled."""
    while True:
        try:
            await run_in_threadpool(_purge_once)
        except Exception as exc:  # keep the loop alive; the next round tries again
            log.warning("session purge failed: %s", type(exc).__name__)  # type only
        await asyncio.sleep(PURGE_EVERY_S)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    task = asyncio.create_task(_purge_forever())
    app.state.purge_task = task
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(title="TenantProof API", version="0.1.0", lifespan=lifespan)
# Issue #10: an upload with a bad intake link, or a caseworker upload without a login, is
# refused before its body is read. Added before ProxyHeadersMiddleware, so it runs inside it
# and sees the visitor's own address.
app.add_middleware(IntakeTokenGate)
# #58: believe X-Forwarded-For/-Proto only from the listed proxies, so the rate limits count each
# visitor's own IP behind nginx. Uvicorn applies the same setting itself when it starts the app;
# this keeps it however the app is served. Applying it twice gives the same client.
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=get_settings().forwarded_allow_ips)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


@app.middleware("http")
async def security_headers(request: Request, call_next):
    """The same three headers on every answer: the UI, the API, errors and downloads. No
    Content-Security-Policy: the UI is one page with inline script and style."""
    response = await call_next(request)
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    if request.url.path.startswith("/i/"):
        response.headers["Referrer-Policy"] = "no-referrer"
    return response


api = FastAPI(title="TenantProof API")
api.state.limiter = limiter
api.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


@api.middleware("http")
async def check_origin(request: Request, call_next):
    """CSRF defence beside SameSite=Lax: a browser-sent Origin on a mutation must be ours.
    Requests without Origin (curl, server-to-server) carry no ambient cookie risk."""
    origin = request.headers.get("origin")
    if request.method in UNSAFE and origin:
        public = urlsplit(get_settings().public_base_url)
        allowed = {f"{public.scheme}://{public.netloc}", f"{request.url.scheme}://{request.url.netloc}"}
        if origin not in allowed:
            return JSONResponse({"detail": "Cross-origin request refused"}, status_code=403)
    return await call_next(request)


api.add_middleware(UploadBodyLimit)  # WS1: caps upload bodies before FastAPI reads them

for module in (
    auth, me, cases, package, brief, evidence, runs, timeline, gaps, sources, form, activity,
    intake, translation, privacy,
):
    api.include_router(module.router)
app.mount("/api", api)

_web = Path(__file__).resolve().parent.parent.parent / "web"
_index = _web / "index.html"


@app.get("/healthz")
def healthz(response: Response) -> dict[str, str]:
    """Readiness: 200 `ok` when Postgres and Redis both answer, else 503 `degraded`. Which one
    failed is logged, not returned: the endpoint is public (`services/health.py`)."""
    if not health.checker.current().ok:
        response.status_code = 503
        return {"status": "degraded", "llm_mode": get_settings().llm_mode}
    return {"status": "ok", "llm_mode": get_settings().llm_mode}


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.get("/")
@app.get("/i/{token}")
def serve_ui(token: str | None = None) -> FileResponse:
    """The same UI serves caseworkers (/) and tenants (/i/{token})."""
    return FileResponse(_index, headers={"Cache-Control": "no-cache"})  # always revalidate


if _web.exists():
    app.mount("/static", StaticFiles(directory=_web), name="static")
