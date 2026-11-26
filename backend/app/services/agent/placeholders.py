"""Personal details out of the model's view (issue #60), wired in at one place: the provider.

`PlaceholderProvider` wraps the configured provider. Before each call it swaps the personal
details in the conversation for placeholders (`services/personal_details.py`); after it, it
swaps the real values back into the reply. Everything else (the tools, the quote checks, what
is saved and shown) keeps working on real text: a quote the model copies with placeholders in
it is the evidence's own words again by the time a tool checks it.

Left as they are: the stable system prompt and tool specs (no case data), and signed thinking
blocks (changing them would break the signature; the model wrote them from placeholder text).

One `Placeholders` is built per run and kept for all its calls, so a value keeps its
placeholder from the first call to the last, even when a form field is filled mid-run.
"""

import json
from collections.abc import Callable
from typing import Any

from sqlmodel import Session, col, select

from app.config import get_settings
from app.models import CaseRow, EvidenceRow, FormFieldRow, PrivacyTermRow
from app.services.agent.evidence_text import EvidenceTextStore
from app.services.agent.llm import LLMProvider, LLMRequest, LLMResponse, Message
from app.services.personal_details import (
    Known,
    Placeholders,
    address_variants,
    caseworker_known,
    name_variants,
)

LEGEND = (
    "Personal details in this case are replaced by placeholders in square brackets, such as "
    "[TENANT_NAME], [RENTAL_ADDRESS] or [PHONE_1]. Each placeholder always stands for the same "
    "real value, and placeholders that share a stem ([TENANT_NAME], [TENANT_NAME_2]) are "
    "spellings of the same person or place. Treat a placeholder as the person, place or number "
    "it stands for. When you quote evidence, fill a form field or write to the tenant, copy "
    "placeholders exactly as written: the app puts the real values back before anyone reads "
    "them. Do not guess the real values. When you write to the tenant, address them by name "
    "with their placeholder, as you would with their real name. English text always uses "
    "[TENANT_NAME] (\"Dear [TENANT_NAME],\"). Text in another language uses the placeholder "
    "of their name as it is written in that language when the evidence has one (such as "
    "[TENANT_NAME_2]), else [TENANT_NAME], with that language's usual greeting."
)

# Form J fields that hold personal details, and the placeholder stem each one gets.
_NAME_FIELDS = {"tenant_name": "TENANT", "landlord_name": "LANDLORD"}
_ADDRESS_FIELDS = {"rental_address": "RENTAL", "landlord_address": "LANDLORD"}
_F1_PREFIXES = {
    "tenant_civic": "TENANT", "tenant_mailing": "TENANT", "landlord_civic": "LANDLORD",
    "landlord_mailing": "LANDLORD", "premises": "RENTAL",
}  # fmt: skip
_CONTACT_FIELDS = {
    "tenant_home_phone": ("TENANT_PHONE", "phone"),
    "tenant_other_phone": ("TENANT_PHONE", "phone"),
    "tenant_email": ("TENANT_EMAIL", "email"),
    "landlord_home_phone": ("LANDLORD_PHONE", "phone"),
    "landlord_other_phone": ("LANDLORD_PHONE", "phone"),
    "landlord_email": ("LANDLORD_EMAIL", "email"),
}


def _address(value: str, prefix: str) -> list[Known]:
    # A bare apartment number ("9") would hide every 9 in the evidence: an address needs letters.
    if len(value.strip()) < 4 or not any(c.isalpha() for c in value):
        return []
    return address_variants(value, prefix)


def case_terms(session: Session, case_id: str) -> list[PrivacyTermRow]:
    return list(session.exec(
        select(PrivacyTermRow).where(PrivacyTermRow.case_id == case_id)
        .order_by(col(PrivacyTermRow.created_at), col(PrivacyTermRow.id))
    ).all())  # fmt: skip


def known_details(session: Session, case: CaseRow, terms: list[PrivacyTermRow]) -> list[Known]:
    """The case's own personal details: the case record first, then the Form J draft, then the
    caseworker's entries. The first spelling of a value wins its placeholder."""
    known = [*name_variants(case.tenant_name, "TENANT"), *_address(case.unit, "RENTAL")]
    fields = session.exec(
        select(FormFieldRow).where(FormFieldRow.case_id == case.id)
        .order_by(col(FormFieldRow.position), col(FormFieldRow.key))
    ).all()  # fmt: skip
    for f in fields:
        value = (f.value or "").strip()
        if not value:
            continue
        if f.key in _NAME_FIELDS:
            known += name_variants(value, _NAME_FIELDS[f.key])
        elif f.key in _ADDRESS_FIELDS:
            known += _address(value, _ADDRESS_FIELDS[f.key])
        elif f.key in _CONTACT_FIELDS:
            stem, kind = _CONTACT_FIELDS[f.key]
            if len(value) >= 4:
                known.append(Known(value, stem, kind, "case", numbered=False))
        else:
            group, _, part = f.key.rpartition("_")
            if group in _F1_PREFIXES and part in ("street", "apartment"):
                known += _address(value, _F1_PREFIXES[group])
    known += [caseworker_known(t.value, t.role, t.id) for t in terms if t.action == "hide"]
    return known


def case_evidence(session: Session, case_id: str) -> list[EvidenceRow]:
    return list(session.exec(
        select(EvidenceRow).where(EvidenceRow.case_id == case_id)
        .order_by(col(EvidenceRow.position), col(EvidenceRow.label))
    ).all())  # fmt: skip


def build(
    session: Session, case: CaseRow, texts: EvidenceTextStore, *, apply_shown: bool = True
) -> Placeholders:
    """The case's placeholders. Every evidence page is read once, in exhibit order, so the
    numbered placeholders ([PHONE_1], [ADDRESS_2]) are the same on every build and match what
    the caseworker sees on the Evidence page. `apply_shown=False` ignores the caseworker's
    "send as written" choices (to list what they would undo)."""
    terms = case_terms(session, case.id)
    shown = [t.value for t in terms if t.action == "show"] if apply_shown else []
    placeholders = Placeholders(known=known_details(session, case, terms), shown=shown)
    for row in case_evidence(session, case.id):
        for page in texts.pages(case.id, row.id):
            placeholders.hide(page.text, count=True)
    return placeholders


# ---------------------------------------------------------------------- the provider wrapper


def _strings(value: Any, fn: Callable[[str], str]) -> Any:
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, list):
        return [_strings(v, fn) for v in value]  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, dict):
        return {k: _strings(v, fn) for k, v in value.items()}  # pyright: ignore[reportUnknownVariableType]
    return value


def _json_text(text: str, fn: Callable[[str], str]) -> str:
    """A tool result is JSON text. Its values are swapped one by one, so a name split by a line
    break (`Mei\\nChen` in the JSON) is still found; anything else is swapped as plain text."""
    try:
        data = json.loads(text)
    except ValueError:
        return fn(text)
    if not isinstance(data, dict | list):
        return fn(text)
    return json.dumps(_strings(data, fn), ensure_ascii=False, separators=(",", ":"))


def _block(block: dict[str, Any], fn: Callable[[str], str]) -> dict[str, Any]:
    kind = block.get("type")
    if kind == "text":
        return {**block, "text": fn(str(block.get("text", "")))}
    if kind == "tool_use":
        return {**block, "input": _strings(block.get("input", {}), fn)}
    if kind == "tool_result":
        content = block.get("content")
        if isinstance(content, str):
            return {**block, "content": _json_text(content, fn)}
        if isinstance(content, list):
            return {**block, "content": [
                _block(b, fn) if isinstance(b, dict) else b  # pyright: ignore[reportUnknownArgumentType]
                for b in content  # pyright: ignore[reportUnknownVariableType]
            ]}
        return block
    # Signed thinking blocks, images, and a chat provider's own history block (written from
    # placeholder text, so it goes back as the model wrote it).
    return block


def _content(content: str | list[dict[str, Any]], fn: Callable[[str], str]) -> Any:
    if isinstance(content, str):
        return fn(content)
    return [_block(b, fn) for b in content]


class PlaceholderProvider:
    """A provider that sends placeholders and returns real values. Anything else (the model
    name, the continuation profile of a paused run) is the wrapped provider's."""

    def __init__(self, inner: LLMProvider, placeholders: Placeholders) -> None:
        self.inner = inner
        self.placeholders = placeholders

    def __getattr__(self, name: str) -> Any:
        if name in ("inner", "placeholders"):  # not set yet (copying): no recursion
            raise AttributeError(name)
        return getattr(self.inner, name)

    def hide_request(self, request: LLMRequest) -> LLMRequest:
        hide = self.placeholders.hide
        return request.model_copy(update={
            "system_run": f"{request.system_run}\n\n{LEGEND}".strip(),
            "messages": [Message(role=m.role, content=_content(m.content, hide))
                         for m in request.messages],
        })  # fmt: skip

    def reveal_response(self, response: LLMResponse) -> LLMResponse:
        reveal = self.placeholders.reveal
        return response.model_copy(update={
            "content": [_block(b, reveal) for b in response.content],
            "text": reveal(response.text),
            "tool_uses": [u.model_copy(update={"input": _strings(u.input, reveal)})
                          for u in response.tool_uses],
        })  # fmt: skip

    async def complete(self, request: LLMRequest) -> LLMResponse:
        return self.reveal_response(await self.inner.complete(self.hide_request(request)))


def for_case(
    session: Session, case: CaseRow, texts: EvidenceTextStore, provider: LLMProvider
) -> LLMProvider:
    """The provider to use for this case: wrapped when PRIVACY_PLACEHOLDERS is on."""
    if not get_settings().privacy_placeholders or isinstance(provider, PlaceholderProvider):
        return provider
    return PlaceholderProvider(provider, build(session, case, texts))
