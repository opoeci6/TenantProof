"""Issue #60: the personal details the AI model does not see, and the caseworker's choices.

The list is rebuilt on every GET from the case, its Form J draft, its evidence text and the
caseworker's entries, the same way a run builds it, so what the caseworker checks here is what
the next run sends. Values are personal data: they are returned to the signed-in caseworker of
the case's org only and never written to the activity log.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, HTTPException, status
from pydantic import ValidationError
from sqlmodel import Session

from app.config import get_settings
from app.deps import CaseDep, SessionDep, UserDep
from app.models import CaseRow, EvidenceRow, PrivacyTermRow
from app.schemas import (
    EvidenceModelText,
    ModelTextPage,
    PersonalDetail,
    PrivacyTermCreate,
    PrivacyView,
)
from app.services import activity
from app.services.agent import placeholders
from app.services.agent.evidence_text import SqlTextStore
from app.services.personal_details import Entry, normalize

router = APIRouter()


def _detail(e: Entry, *, shown: bool = False, term_id: str | None = None) -> PersonalDetail:
    return PersonalDetail(
        value=e.value, placeholder=e.placeholder, kind=e.kind,  # pyright: ignore[reportArgumentType]
        source=e.source, count=e.count, shown=shown,  # pyright: ignore[reportArgumentType]
        term_id=term_id or e.term_id,
    )


def privacy_view(session: Session, case: CaseRow) -> PrivacyView:
    texts = SqlTextStore(session)
    built = placeholders.build(session, case, texts)
    details = [_detail(e) for e in built.entries()]
    shown = [t for t in placeholders.case_terms(session, case.id) if t.action == "show"]
    if shown:  # what each value let through would have been, so the caseworker can undo it
        hidden = placeholders.build(session, case, texts, apply_shown=False)
        by_value: dict[str, Entry] = {}
        for e in hidden.entries():
            by_value.setdefault(normalize(e.value), e)
        for t in shown:
            e = by_value.get(t.value_key)
            details.append(
                _detail(e, shown=True, term_id=t.id) if e is not None else PersonalDetail(
                    value=t.value, placeholder="", kind="other", source="caseworker", count=0,
                    shown=True, term_id=t.id,
                )
            )  # fmt: skip
    return PrivacyView(enabled=get_settings().privacy_placeholders, details=details)


@router.get("/cases/{case_id}/privacy", response_model=PrivacyView)
def get_privacy(case: CaseDep, session: SessionDep) -> PrivacyView:
    """The personal details found in the case and the placeholder the model sees for each.
    With the setting off the UI shows no list, so none is built: building it reads and scans
    every evidence page, on every load of the case (issue #165)."""
    if not get_settings().privacy_placeholders:
        return PrivacyView(enabled=False, details=[])
    return privacy_view(session, case)


def _parse(raw: Any) -> PrivacyTermCreate:
    try:
        return PrivacyTermCreate.model_validate(raw)
    except ValidationError:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            {"reasons": ["Enter 2 to 200 characters, choose hide or show, and what it is"]},
        ) from None


@router.post("/cases/{case_id}/privacy/terms", response_model=PrivacyView)
def add_privacy_term(
    case: CaseDep, session: SessionDep, user: UserDep, body: Annotated[Any, Body()] = None
) -> PrivacyView:
    """Hide a value the finder missed, or let a found value go to the model as written. A second
    choice for the same value (any case or spacing) replaces the first."""
    payload = _parse(body)
    key = normalize(payload.value)
    row = next((t for t in placeholders.case_terms(session, case.id) if t.value_key == key), None)
    if row is None:
        row = PrivacyTermRow(
            id=f"pt_{uuid.uuid4().hex[:10]}", case_id=case.id, value=payload.value,
            value_key=key, action=payload.action, role=payload.role, created_by=user.name,
        )  # fmt: skip
    else:
        row.value, row.action, row.role, row.created_by = (
            payload.value, payload.action, payload.role, user.name)
    session.add(row)
    activity.log(
        session, case.id, "caseworker", user.name,
        "Hid a personal detail from the AI model" if payload.action == "hide"
        else "Let a personal detail go to the AI model as written",
    )  # fmt: skip
    session.commit()
    return privacy_view(session, case)


@router.delete("/cases/{case_id}/privacy/terms/{term_id}", response_model=PrivacyView)
def delete_privacy_term(
    term_id: str, case: CaseDep, session: SessionDep, user: UserDep
) -> PrivacyView:
    """Undo a choice: the value is treated as the finder alone would treat it."""
    row = session.get(PrivacyTermRow, term_id)
    if row is None or row.case_id != case.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    session.delete(row)
    activity.log(session, case.id, "caseworker", user.name, "Undid a personal-detail choice")
    session.commit()
    return privacy_view(session, case)


@router.get(
    "/cases/{case_id}/evidence/{evidence_id}/model-text", response_model=EvidenceModelText
)
def get_model_text(evidence_id: str, case: CaseDep, session: SessionDep) -> EvidenceModelText:
    """An evidence item's text as the model receives it: with placeholders when
    PRIVACY_PLACEHOLDERS is on, as written when it is off."""
    row = session.get(EvidenceRow, evidence_id)
    if row is None or row.case_id != case.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    texts = SqlTextStore(session)
    pages = texts.pages(case.id, row.id)
    if get_settings().privacy_placeholders:
        built = placeholders.build(session, case, texts)
        pages = [type(p)(p.locator, built.hide(p.text)) for p in pages]
    return EvidenceModelText(
        evidence_id=row.id, label=row.label,
        pages=[ModelTextPage(locator=p.locator, text=p.text) for p in pages],
    )  # fmt: skip
