from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Annotated, Any

import segno
from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.deps import CaseDep, SessionDep, UserDep
from app.models import UserRow
from app.schemas import Case, CaseCreate, CaseSummary, Intake
from app.services import activity
from app.services import cases as cases_service
from app.services.intake_token import issue_token
from app.services.mappers import list_case_rows, to_case, to_intake, to_summary

# For the activity log a caseworker reads: the English name first, the native one after it (#48).
LANG_NAMES = {
    "en": "English", "zh": "Chinese (中文)", "fr": "French (Français)", "es": "Spanish (Español)",
    "ru": "Russian (Русский)", "hi": "Hindi (हिन्दी)", "ar": "Arabic (العربية)",
}  # fmt: skip

router = APIRouter()


@router.get("/cases", response_model=list[CaseSummary])
def list_cases(session: SessionDep, user: UserDep) -> list[CaseSummary]:
    today = date.today()
    ranked = [
        to_summary(case, blocking, pending, last_at, today, analysed)
        for case, blocking, pending, last_at, analysed in list_case_rows(session, user.org_id)
    ]
    ranked.sort(key=lambda pair: pair[1])
    return [summary for summary, _ in ranked]


def _plain(err: Mapping[str, Any]) -> str:
    """One validation error in the caseworker's words, never echoing the rejected value."""
    if not err["loc"]:  # not a JSON object at all
        return "Send the tenant name, language, unit address and what is wrong"
    field = str(err["loc"][-1])
    label = cases_service.LABELS.get(field)
    if label is None:
        return f"{field[:40]}: not a field of a new case"
    kind, ctx = err["type"], err.get("ctx") or {}
    if field == "tenant_language" and kind in ("missing", "literal_error"):
        return f"{label}: choose English, 中文, Français, Español, Русский or हिन्दी"
    if kind in ("missing", "string_too_short"):
        return f"{label}: enter some text"
    if kind == "string_too_long":
        return f"{label}: at most {ctx.get('max_length')} characters"
    return f"{label}: not valid"


@router.post("/cases", response_model=Case)
def create_case(
    session: SessionDep, user: UserDep, body: Annotated[Any, Body()] = None
) -> Case:
    """Create a case in the caseworker's own org, assigned to them (CONTRACT_CHANGES #35). The
    body is validated here, not by FastAPI, so a bad one answers 422 `{ detail: { reasons } }`
    like the other forms the UI shows errors for."""
    try:
        parsed = CaseCreate.model_validate(body)
    except ValidationError as exc:
        reasons = list(dict.fromkeys(_plain(err) for err in exc.errors()))
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": reasons}) from None
    refused = cases_service.problems(parsed)
    if refused:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": refused})
    case = cases_service.create_case(session, user, parsed)
    session.commit()
    session.refresh(case)
    return to_case(case, user.name)


@router.get("/cases/{case_id}", response_model=Case)
def get_case(case: CaseDep, session: SessionDep) -> Case:
    assignee = session.get(UserRow, case.assigned_to_id)
    return to_case(case, assignee.name if assignee else "")


def qr_data_uri(url: str) -> str:
    """#56: the link as a QR code, an SVG data URI. Black on white with the standard 4-module
    margin, so a phone can scan it from a dark screen too."""
    return segno.make(url, error="m").svg_data_uri(scale=4, border=4, dark="#000", light="#fff")


@router.post("/cases/{case_id}/intake-link", response_model=Intake)
def send_intake_link(
    case: CaseDep,
    session: SessionDep,
    user: UserDep,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Intake:
    """Create (or re-send) the tenant link. The raw URL is returned only here; earlier links
    are revoked. Only a hash is stored."""
    first_send = case.intake_status == "not_sent"
    token = issue_token(session, case.id, settings.intake_token_ttl_days)
    case.intake_sent_at = datetime.now(UTC)
    if first_send:
        case.intake_status = "sent"
    lang = LANG_NAMES.get(case.tenant_language, case.tenant_language)
    verb = "Sent" if first_send else "Re-sent"
    activity.log(session, case.id, "caseworker", user.name, f"{verb} the intake link in {lang}")
    session.add(case)
    session.commit()
    session.refresh(case)
    url = f"{settings.public_base_url}/i/{token}"
    return to_intake(case, url=url, qr_svg=qr_data_uri(url))
