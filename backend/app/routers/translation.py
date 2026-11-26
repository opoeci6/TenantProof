"""Caseworker reading aid: translate one quoted source into English (issue #96).

One model call per quote, cached by language and excerpt; the translation is shown next to the
quote and is never cited, filed, or used to check the quote. Third-party text goes to the model
inside an evidence block (the service does the wrapping).
"""

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.config import get_settings
from app.deps import CaseDep, SessionDep, UserDep
from app.models import ExcerptTranslationRow
from app.ratelimit import limiter
from app.schemas import ExcerptTranslation, ExcerptTranslationRequest
from app.services import activity, excerpt_translation
from app.services.agent import llm, placeholders, prompts
from app.services.agent.evidence_text import SqlTextStore

router = APIRouter()

TRANSLATE_RATE_LIMIT = "20/minute"  # a miss is one model call; cache hits are cheap


def _refuse(*reasons: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": list(reasons)})


def translate_provider() -> llm.LLMProvider:
    """This deployment's provider (`LLM_MODE`). Without a usable setup, 503 with a plain reason.
    Tests override this dependency."""
    try:
        return llm.get_provider(get_settings())
    except llm.LLMConfigError:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, {"reasons": [excerpt_translation.NO_MODEL]}
        ) from None


TranslateProviderDep = Annotated[llm.LLMProvider, Depends(translate_provider)]


def _parse(raw: Any) -> ExcerptTranslationRequest:
    try:
        return ExcerptTranslationRequest.model_validate(raw)
    except ValidationError:
        raise _refuse(
            "The quote must be text of 1 to 3000 characters; the language is optional"
        ) from None


@router.post("/cases/{case_id}/translate-excerpt", response_model=ExcerptTranslation)
@limiter.limit(TRANSLATE_RATE_LIMIT)
def translate_excerpt(
    request: Request,  # noqa: ARG001 (slowapi reads it)
    case: CaseDep,
    session: SessionDep,
    user: UserDep,
    provider: TranslateProviderDep,
    body: Annotated[Any, Body()] = None,
) -> ExcerptTranslation:
    """A non-English quote, in English, for the caseworker's eyes only."""
    payload = _parse(body)
    language = (payload.language or case.tenant_language).casefold()
    if language not in prompts.LANGUAGE_NAMES:
        raise _refuse(f"Unknown language: {language}")
    if excerpt_translation.unchanged(language, payload.excerpt):
        return ExcerptTranslation(text_en=payload.excerpt)
    text_en = excerpt_translation.cached(session, language, payload.excerpt)
    if text_en is not None:
        return ExcerptTranslation(text_en=text_en)
    provider = placeholders.for_case(session, case, SqlTextStore(session), provider)  # issue #60
    # Give the connection back while the model runs: a slow call must not hold a DB connection.
    session.commit()
    text_en = asyncio.run(excerpt_translation.translate(provider, language, payload.excerpt))
    _save(session, case.id, user.name, language, payload.excerpt, text_en)
    return ExcerptTranslation(text_en=text_en)


def _save(
    session: Session, case_id: str, actor_name: str, language: str, excerpt: str, text_en: str
) -> None:
    """Cache the translation and log it. Two requests for the same new quote can race; the second
    insert loses and keeps the translation the first one saved."""
    session.add(
        ExcerptTranslationRow(
            key=excerpt_translation.key_for(language, excerpt),
            language=language,
            text_en=text_en,
        )
    )
    activity.log(session, case_id, "caseworker", actor_name, "Translated a quoted source")
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
