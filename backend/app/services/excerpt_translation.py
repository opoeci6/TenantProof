"""Machine translation of a quoted source (issue #96).

A reading aid for a caseworker who cannot read the quote's language. It is never cited, filed, or
used to check a quote, and it is cached by language and excerpt text so the same quote is paid for
once. The quote goes to the model wrapped as evidence: third-party text, not instructions.
"""

import hashlib
import logging

from fastapi import HTTPException, status
from sqlmodel import Session

from app.models import ExcerptTranslationRow
from app.services.agent import llm, prompts
from app.services.agent.tools import wrap_evidence

log = logging.getLogger(__name__)

MAX_EXCERPT = 3000
TRANSLATE_MAX_TOKENS = 8000
NO_MODEL = "Translation is not available right now. The original quote is unchanged below."


def key_for(language: str, excerpt: str, case_id: str | None = None) -> str:
    """The cache key: the same words in the same language are translated once. With a `case_id`
    the entry belongs to that case only: with PRIVACY_PLACEHOLDERS on, the model's reply is
    revealed with the case's own details (issue #60), so it must not be served to another org."""
    scope = f"case:{case_id}\n" if case_id else ""
    return hashlib.sha256(f"{scope}{language}\n{excerpt}".encode()).hexdigest()


def unchanged(language: str, excerpt: str) -> bool:  # noqa: ARG001
    """Nothing to translate when the language is English."""
    return language == "en"


def cached(
    session: Session, language: str, excerpt: str, case_id: str | None = None
) -> str | None:
    row = session.get(ExcerptTranslationRow, key_for(language, excerpt, case_id))
    return row.text_en if row is not None else None


async def translate(provider: llm.LLMProvider, language: str, excerpt: str) -> str:
    """One model call for an excerpt that is not cached. 503 with a plain reason on failure."""
    _tag, block = wrap_evidence("quoted-source", excerpt, locator="quoted source")
    request = llm.LLMRequest(
        system=prompts.translate_excerpt_system(),
        messages=[
            llm.Message(
                role="user",
                content=prompts.translate_excerpt_request(language, block),
            )
        ],
        max_tokens=TRANSLATE_MAX_TOKENS,
        fixture_key=prompts.TRANSLATE_EXCERPT_FIXTURE_KEY,
    )
    try:
        response = await provider.complete(request)
    except llm.LLMError as exc:
        log.warning("excerpt translation failed: %s", type(exc).__name__)  # type only
        raise _no_model() from None
    text = response.text.strip()
    if response.stop_reason != "end_turn" or not text:
        log.warning("excerpt translation stopped: %s", response.stop_reason)
        raise _no_model()
    return text


def _no_model() -> HTTPException:
    return HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"reasons": [NO_MODEL]})
