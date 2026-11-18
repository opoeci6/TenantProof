import asyncio
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import BaseModel, ValidationError
from sqlmodel import col, select

from app.config import get_settings
from app.deps import CaseDep, SessionDep, UserDep, load_owned
from app.models import CaseRow, GapRow
from app.ratelimit import limiter
from app.schemas import (
    Gap,
    GapPatch,
    TenantRequestSend,
    TenantRequestTranslate,
    TenantRequestTranslation,
)
from app.services import review, tenant_request
from app.services.agent import llm
from app.services.agent.evidence_text import SqlTextStore
from app.services.mappers import to_gap

router = APIRouter()

TRANSLATE_RATE_LIMIT = "20/minute"  # each one is a model call


def translate_provider() -> llm.LLMProvider:
    """The model for translations: this deployment's provider (`LLM_MODE`). Without a usable
    setup, 503 with a plain reason (CONTRACT_CHANGES #37). Tests override this dependency."""
    try:
        return llm.get_provider(get_settings())
    except llm.LLMConfigError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, {"reasons": [tenant_request.NO_MODEL]}
        ) from None


TranslateProviderDep = Annotated[llm.LLMProvider, Depends(translate_provider)]


@router.get("/cases/{case_id}/gaps", response_model=list[Gap])
def list_gaps(case: CaseDep, session: SessionDep) -> list[Gap]:
    rows = session.exec(
        select(GapRow).where(GapRow.case_id == case.id).order_by(col(GapRow.position))
    ).all()
    return [to_gap(r) for r in rows]


def _load(gap_id: str, session: SessionDep, user: UserDep) -> tuple[GapRow, CaseRow]:
    gap = load_owned(GapRow, gap_id, session, user)
    case = session.get(CaseRow, gap.case_id)
    assert case is not None
    return gap, case


@router.patch("/gaps/{gap_id}", response_model=Gap)
def patch_gap(gap_id: str, body: GapPatch, session: SessionDep, user: UserDep) -> Gap:
    gap, case = _load(gap_id, session, user)
    review.patch_gap(session, case, gap, body, user)
    session.commit()
    session.refresh(gap)
    return to_gap(gap)


def _plain(err: Mapping[str, Any]) -> str:
    """One validation error in the caseworker's words, never echoing the rejected value."""
    if not err["loc"]:
        return "Send the request as message and message_en"
    field = str(err["loc"][-1])
    label = tenant_request.LABELS.get(field)
    if label is None:
        return f"{field[:40]}: not part of a request to the tenant"
    kind, ctx = err["type"], err.get("ctx") or {}
    if kind in ("missing", "string_too_short"):
        return f"{label}: enter some text"
    if kind == "string_too_long":
        return f"{label}: at most {ctx.get('max_length')} characters"
    return f"{label}: not valid"


def _parse[M: BaseModel](model: type[M], body: Any) -> M:
    """Validated here, not by FastAPI, so a bad body answers 422 `{ detail: { reasons } }`."""
    try:
        return model.model_validate(body)
    except ValidationError as exc:
        reasons = list(dict.fromkeys(_plain(err) for err in exc.errors()))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": reasons}) from None


@router.post("/gaps/{gap_id}/tenant-request", response_model=Gap)
def send_tenant_request(
    gap_id: str, session: SessionDep, user: UserDep, body: Annotated[Any, Body()] = None
) -> Gap:
    """Send the drafted request through the intake link. With a body `{ message, message_en }`
    the caseworker's edited version is checked, saved and sent (CONTRACT_CHANGES #37)."""
    gap, case = _load(gap_id, session, user)
    if body is None:
        review.request_from_tenant(session, case, gap, user)
    else:
        tenant_request.check_gap(gap)  # 409 before 422: nothing to edit
        edit = _parse(TenantRequestSend, body)
        clean = tenant_request.checked(
            session, SqlTextStore(session), case, message=edit.message, message_en=edit.message_en
        )
        tenant_request.send_edited(
            session, case, gap, user, clean["message"], clean["message_en"]
        )
    session.commit()
    session.refresh(gap)
    return to_gap(gap)


@router.post("/gaps/{gap_id}/tenant-request/translate", response_model=TenantRequestTranslation)
@limiter.limit(TRANSLATE_RATE_LIMIT)
def translate_tenant_request(
    request: Request,
    gap_id: str,
    session: SessionDep,
    user: UserDep,
    provider: TranslateProviderDep,
    body: Annotated[Any, Body()] = None,
) -> TenantRequestTranslation:
    """The caseworker's English in the tenant's language, checked like an edit. Saves nothing.
    For an English-speaking tenant the English comes back as it is, with no model call."""
    gap, case = _load(gap_id, session, user)
    tenant_request.check_gap(gap)
    asked = _parse(TenantRequestTranslate, body)
    texts = SqlTextStore(session)
    english = tenant_request.checked(session, texts, case, message_en=asked.message_en)
    message_en = english["message_en"]
    language = case.tenant_language
    if language.casefold().split("-")[0] == "en":
        return TenantRequestTranslation(message=message_en)
    session.commit()  # end the read transaction: no connection held through the model call
    raw = asyncio.run(tenant_request.translate(provider, language, message_en))
    raw = tenant_request.unwrapped(raw)
    clean = tenant_request.checked(session, texts, case, message=raw, message_en=message_en)
    return TenantRequestTranslation(message=clean["message"])
