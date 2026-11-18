"""The request to the tenant as the caseworker edits it (issue #48, CONTRACT_CHANGES #37).

The agent drafts each request (`draft_tenant_message`) in the tenant's language and in English.
Before sending, the caseworker can change either text, and can have the English translated by one
model call. An edited text gets the same checks as the agent's draft: a quotation must be in this
case's evidence, a long run copied from it must be quoted, a non-English message must be in the
tenant's language, and "<" is refused (escaped, it would reach the tenant as "&lt;").
"""

import logging
import re

from fastapi import HTTPException, status
from sqlmodel import Session

from app.models import CaseRow, GapRow, UserRow
from app.services import activity
from app.services.agent import llm, prompts
from app.services.agent.evidence_text import EvidenceTextStore
from app.services.agent.tools import MESSAGE_MAX, case_pages, copy_problem, language_problem
from app.services.cases import ANGLES, blank, clean_typed
from app.services.review import OPEN, request_from_tenant

log = logging.getLogger(__name__)

# A 3000-character translation is under 3000 tokens; the rest is room for thinking, which counts
# against max_tokens. Unused tokens cost nothing.
TRANSLATE_MAX_TOKENS = 8000
_WRAPPER = re.compile(r"^\s*<message>\s*(.*?)\s*</message>\s*$", re.S)
LABELS = {"message": "Tenant's language", "message_en": "English"}
# The TenantRequestSend limits. The tenant's text may be longer: a translation can run longer.
LIMITS = {"message": 2 * MESSAGE_MAX, "message_en": MESSAGE_MAX}
NO_MODEL = (
    "Translation is not available right now. Edit the text in the tenant's language yourself."
)


def _refuse(code: int, *reasons: str) -> HTTPException:
    return HTTPException(code, {"reasons": list(reasons)})


def check_gap(gap: GapRow) -> dict[str, str]:
    """The stored draft of a gap that can still be sent. 409 otherwise, as for a plain send."""
    if gap.tenant_request is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No drafted message for this item")
    if gap.status not in OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, "This item is already closed")
    return gap.tenant_request


def _language_reason(language: str) -> str:
    name = prompts.LANGUAGE_NAMES.get(language.casefold().split("-")[0], language)
    return (
        f"{LABELS['message']}: write it in {name}. Translate the English, or write the "
        f"{name} yourself."
    )


def checked(
    session: Session, texts: EvidenceTextStore, case: CaseRow, **values: str
) -> dict[str, str]:
    """The cleaned texts (`message`, `message_en`, or one of them), or 422 with one plain reason
    per problem. `message` is checked against `message_en` for the language when both are given."""
    reasons = [
        f"{LABELS[key]}: remove the < character"
        for key, value in values.items()
        if any(angle in value for angle in ANGLES)
    ]
    if reasons:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_CONTENT, *reasons)
    # Cut well past the limit, so a long text is refused below, never shortened without a word.
    clean = {key: clean_typed(value, 2 * LIMITS[key]) for key, value in values.items()}
    for key, value in clean.items():
        if blank(value):
            reasons.append(f"{LABELS[key]}: enter some text")
        elif len(value) > LIMITS[key]:
            reasons.append(f"{LABELS[key]}: at most {LIMITS[key]} characters")
    if reasons:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_CONTENT, *reasons)
    pages = case_pages(session, texts, case.id)
    for key, value in clean.items():
        problem = copy_problem(value, pages)
        if problem is not None:
            reasons.append(f"{LABELS[key]}: {problem}")
    if "message" in clean and "message_en" in clean:
        if language_problem(case.tenant_language, clean["message"], clean["message_en"]):
            reasons.append(_language_reason(case.tenant_language))
    if reasons:
        raise _refuse(status.HTTP_422_UNPROCESSABLE_CONTENT, *reasons)
    return clean


def send_edited(
    session: Session, case: CaseRow, gap: GapRow, user: UserRow, message: str, message_en: str
) -> GapRow:
    """Save the caseworker's version (already `checked`) and send it. Unchanged text is sent
    as a plain approval. The agent's first draft stays on the gap, for the record."""
    stored = check_gap(gap)
    if (message, message_en) != (stored.get("message"), stored.get("message_en")):
        first = stored.get("agent_draft") or {
            "message": stored.get("message", ""), "message_en": stored.get("message_en", ""),
        }  # fmt: skip
        gap.tenant_request = {
            "language": case.tenant_language, "message": message, "message_en": message_en,
            "agent_draft": first,
        }  # fmt: skip
        session.add(gap)
        activity.log(session, case.id, "caseworker", user.name,
                     f"Edited the request to the tenant: {gap.title}")  # fmt: skip
    return request_from_tenant(session, case, gap, user)


def unwrapped(raw: str) -> str:
    """The model's translation as text for the tenant: the <message> tags it may echo from the
    request are dropped, and any other "<" becomes a space (it is the model's, not typed)."""
    match = _WRAPPER.match(raw)
    text = match.group(1) if match else raw
    return text.translate({ord(a): " " for a in ANGLES})


async def translate(provider: llm.LLMProvider, language: str, message_en: str) -> str:
    """One model call: the caseworker's English in the tenant's language. Nothing is saved; the
    caller checks the result like an edit. 503 with a plain reason when the model fails."""
    request = llm.LLMRequest(
        system=prompts.translate_system(),
        messages=[
            llm.Message(role="user", content=prompts.translate_request(language, message_en))
        ],
        max_tokens=TRANSLATE_MAX_TOKENS,
        fixture_key=prompts.TRANSLATE_FIXTURE_KEY,
    )
    try:
        response = await provider.complete(request)
    except llm.LLMError as exc:
        log.warning("translation failed: %s", type(exc).__name__)  # type only
        raise _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, NO_MODEL) from None
    text = response.text.strip()
    if response.stop_reason != "end_turn" or not text:
        log.warning("translation stopped: %s", response.stop_reason)
        raise _refuse(status.HTTP_503_SERVICE_UNAVAILABLE, NO_MODEL)
    return text
