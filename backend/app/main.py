from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
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
    runs,
    sources,
    timeline,
)
from app.services.evidence.http import UploadBodyLimit

app = FastAPI(title="TenantProof API", version="0.1.0")

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
    intake,
):
    api.include_router(module.router)
app.mount("/api", api)

_web = Path(__file__).resolve().parent.parent.parent / "web"
_index = _web / "index.html"


@app.get("/healthz")
def healthz() -> dict[str, str]:
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
