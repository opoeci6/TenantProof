"""Caseworker mutations: timeline events, gaps, form fields, tenant requests, tenant submit.
Every function logs activity and refreshes the stage; the router commits."""

from datetime import UTC, date, datetime

from fastapi import HTTPException, status
from sqlmodel import Session, select

from app.models import CaseRow, FormFieldRow, GapRow, TimelineEventRow, UserRow
from app.schemas import FieldPatch, GapPatch, TimelineEventPatch
from app.services import activity
from app.services.agent.tools import CLEARED_NOTE, DRAFTED_NOTE
from app.services.form_fields import (
    F2_ALTERNATIVES,
    LEGACY_ALTERNATIVES,
    REQUEST_SELECTION_KEYS,
    covered_legacy_field_for_case,
    field_allowed_for_case,
    lock_alternative_fields,
    materialize_optional_field,
)
from app.services.form_values import (
    DATE_ORDER_FIELDS,
    DATE_ORDER_PAIRS,
    EMAIL_KEYS,
    F2_DATE_KEYS,
    FORM_CHOICES,
    MIN_TENANCY_YEAR,
    MONEY_KEYS,
    RENT_KEYS,
    cad_amount,
    dates_out_of_order,
    is_email_address,
    is_tenancy_start_date,
)
from app.services.legal.gap_checks import UNATTESTED_TITLE
from app.services.stage import refresh_stage
from app.services.verifier import normalize

OPEN = ("open", "waiting_tenant")
CONTENT_FIELDS = ("title", "description", "date")
VERB = {"accepted": "Accepted", "edited": "Edited", "rejected": "Rejected", "pending": "Reset"}
_F2_RESOLUTION_NOTES = {
    "weekly_rent": "Weekly rent saved on the form draft",
    "move_in_date": "Move-in date saved on the form draft",
}


def _bad(detail: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail)


def _date_changed(event: TimelineEventRow, patch: TimelineEventPatch) -> bool:
    """The UI resends the unchanged date on every edit; only a real change becomes basis=user."""
    assert patch.date is not None
    return (event.date.get("value"), event.date.get("precision")) != (
        patch.date.value, patch.date.precision)


def patch_event(
    session: Session, case: CaseRow, event: TimelineEventRow, patch: TimelineEventPatch,
    user: UserRow,
) -> TimelineEventRow:
    sent = patch.model_fields_set
    for key in ("title", "description"):
        if key in sent and not (getattr(patch, key) or "").strip():
            raise _bad(f"{key} cannot be empty")
    edited = False
    if "title" in sent and patch.title is not None and patch.title.strip() != event.title:
        event.title, edited = patch.title.strip(), True
    if (
        "description" in sent and patch.description is not None
        and patch.description.strip() != event.description
    ):
        event.description, edited = patch.description.strip(), True
    if "date" in sent and patch.date is not None and _date_changed(event, patch):
        event.date = {**patch.date.model_dump(), "basis": "user"}
        event.sort_key = patch.date.value or "9"
        edited = True
    if "caseworker_note" in sent:
        event.caseworker_note = (patch.caseworker_note or "").strip() or None
    new_status = patch.review_status if "review_status" in sent else None
    if new_status is None and edited and event.review_status != "rejected":
        new_status = "edited"  # a rejected event needs an explicit review_status to come back
    if new_status in ("accepted", "edited") and not event.sources:
        # API_CONTRACT design rule 2: no source, no fact. Discard the in-memory edits.
        session.rollback()
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, {"reasons": ["needs at least one source"]}
        )
    if new_status is not None:
        event.review_status = new_status
        event.reviewed_by = None if new_status == "pending" else user.name
    session.add(event)
    verb = VERB.get(new_status or "", "Noted on")
    activity.log(session, case.id, "caseworker", user.name, f"{verb} timeline event: {event.title}")
    refresh_stage(session, case)
    return event


def save_field_value(field: FormFieldRow, value: str) -> None:
    """The one way a caseworker's value reaches a form field: the form page and a gap's input
    both come here. A source stays only while its excerpt still holds the value, so a changed
    value never shows a quote that contradicts it. The agent's "confirm it" note is done once the
    caseworker saves; clearing a drafted field marks it so the agent does not draft it again.
    Caller adds and commits."""
    if value != (field.value or ""):
        # Existing verbatim drafts can still be confirmed, and an empty value clears the field.
        if value:
            if field.key in REQUEST_SELECTION_KEYS and value != "yes":
                raise _bad("Select the request or leave it unchecked")
            if field.key in EMAIL_KEYS and not is_email_address(value):
                raise _bad("Enter a valid email address")
            if field.key in MONEY_KEYS and cad_amount(value) is None:
                raise _bad(
                    f"Enter {field.label.lower()} as a positive CAD amount with up to two decimals"
                )
            choices = FORM_CHOICES.get(field.key)
            if choices is not None and value not in choices:
                raise _bad(f"{field.label} must be one of: {', '.join(choices.values())}")
            if field.key in F2_DATE_KEYS and not is_tenancy_start_date(value):
                raise _bad(
                    f"Enter a valid {field.label.lower()} in YYYY-MM-DD format "
                    f"({MIN_TENANCY_YEAR} or later)"
                )
            if field.key == "tenancy_start":
                current_year = date.today().year
                if not is_tenancy_start_date(value, current_year):
                    raise _bad(
                        f"Enter a valid tenancy start date in YYYY-MM-DD format "
                        f"with a year from {MIN_TENANCY_YEAR} to {current_year}"
                    )
        wanted = normalize(value)
        field.sources = [
            s for s in field.sources
            if wanted and wanted in normalize(str(s.get("excerpt") or ""))
        ]  # fmt: skip
    if field.note == DRAFTED_NOTE:
        field.note = None if value else CLEARED_NOTE
    field.value = value or None
    field.status = "user_entered" if value else "missing"


def _field_for_gap(session: Session, gap: GapRow) -> FormFieldRow | None:
    key = (gap.input or {}).get("field_key")
    return session.get(FormFieldRow, (gap.case_id, key)) if key else None


def _check_rent_period(session: Session, case_id: str, key: str, value: str) -> None:
    if value and key in RENT_KEYS:
        other = "weekly_rent" if key == "monthly_rent" else "monthly_rent"
        existing = session.get(FormFieldRow, (case_id, other))
        if existing is not None and existing.value:
            raise _bad(f"Clear {existing.label.lower()} before saving a different rent period")


def _check_date_order(session: Session, field: FormFieldRow, value: str) -> None:
    pair = DATE_ORDER_FIELDS.get(field.key)
    if not pair or not value or value == field.value:
        return
    start, end = pair
    peer_key = end if field.key == start else start
    peer = session.get(FormFieldRow, (field.case_id, peer_key))
    other = peer.value if peer else None
    dates = (value, other) if field.key == start else (other, value)
    if dates_out_of_order(*dates):
        raise _bad(DATE_ORDER_PAIRS[pair])


def patch_gap(
    session: Session, case: CaseRow, gap: GapRow, patch: GapPatch, user: UserRow
) -> GapRow:
    key = (gap.input or {}).get("field_key")
    if key and not field_allowed_for_case(case, key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field not found")
    if key in LEGACY_ALTERNATIVES or key in F2_ALTERNATIVES or key in DATE_ORDER_FIELDS:
        lock_alternative_fields(session, case.id)
        session.refresh(gap)
    was_resolved = gap.status == "resolved"
    if patch.value is not None:
        field = _field_for_gap(session, gap)
        if field is None:
            raise _bad("This item has no form field to fill")
        if patch.status != "resolved":
            raise _bad("A value can only be saved when the item is resolved")
        value = patch.value.strip()
        if not value:
            raise _bad("value cannot be empty")
        _check_rent_period(session, case.id, field.key, value)
        _check_date_order(session, field, value)
        save_field_value(field, value)
        session.add(field)
    elif patch.status in OPEN and was_resolved:
        if key in LEGACY_ALTERNATIVES and covered_legacy_field_for_case(session, case.id, key):
            raise _bad("Clear the corresponding Form J field before reopening this item")
        field = _field_for_gap(session, gap)
        if field is not None and field.status == "user_entered":
            save_field_value(field, "")  # reopen clears what this gap wrote
            session.add(field)
    gap.status = patch.status
    if "resolution_note" in patch.model_fields_set:
        gap.resolution_note = patch.resolution_note
    elif patch.status in OPEN:
        gap.resolution_note = None
    session.add(gap)
    label = {"resolved": "Resolved", "dismissed": "Dismissed", "open": "Reopened",
             "waiting_tenant": "Marked waiting on tenant"}[patch.status]
    activity.log(session, case.id, "caseworker", user.name, f"{label} item: {gap.title}")
    refresh_stage(session, case)
    return gap


def request_sent_line(gap_title: str) -> str:
    """The activity line of a sent request. It is the only record once a gap is reopened."""
    return f"Sent a request to the tenant: {gap_title}"


def request_from_tenant(session: Session, case: CaseRow, gap: GapRow, user: UserRow) -> GapRow:
    if gap.tenant_request is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "No drafted message for this item")
    if gap.status not in OPEN:
        raise HTTPException(status.HTTP_409_CONFLICT, "This item is already closed")
    gap.status = "waiting_tenant"
    # When it was sent: an answer from before this answers an earlier request
    # (mappers.answered_since_sent).
    gap.tenant_request = {**gap.tenant_request, "sent_at": datetime.now(UTC).isoformat()}
    session.add(gap)
    activity.log(session, case.id, "caseworker", user.name, request_sent_line(gap.title))
    refresh_stage(session, case, waiting_on_tenant=True)
    return gap


def patch_field(
    session: Session, case: CaseRow, key: str, patch: FieldPatch, user: UserRow
) -> FormFieldRow:
    if not field_allowed_for_case(case, key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field not found")
    if key in LEGACY_ALTERNATIVES or key in F2_ALTERNATIVES or key in DATE_ORDER_FIELDS:
        lock_alternative_fields(session, case.id)
    field = session.get(FormFieldRow, (case.id, key))
    if field is None:
        field = materialize_optional_field(session, case.id, key)
    if field is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Field not found")
    value = patch.value.strip()
    _check_rent_period(session, case.id, key, value)
    _check_date_order(session, field, value)
    save_field_value(field, value)
    session.add(field)
    for gap in session.exec(select(GapRow).where(GapRow.case_id == case.id)).all():
        gap_key = (gap.input or {}).get("field_key")
        if key in F2_ALTERNATIVES and gap_key == F2_ALTERNATIVES[key]:
            if value and gap.status in OPEN:
                gap.status, gap.resolution_note = "resolved", _F2_RESOLUTION_NOTES[key]
                session.add(gap)
            elif not value and gap.resolution_note == _F2_RESOLUTION_NOTES[key]:
                legacy = session.get(FormFieldRow, (case.id, gap_key))
                if legacy is None or not legacy.value:
                    gap.status, gap.resolution_note = "open", None
                else:
                    gap.resolution_note = f"Saved: {legacy.value}"
                session.add(gap)
            continue
        if gap_key != key:
            continue
        if value and gap.status in OPEN:
            gap.status, gap.resolution_note = "resolved", f"Saved: {value}"
            session.add(gap)
        elif not value and gap.status == "resolved":
            if covered_legacy_field_for_case(session, case.id, key):
                gap.resolution_note = _F2_RESOLUTION_NOTES[LEGACY_ALTERNATIVES[key]]
            else:
                gap.status, gap.resolution_note = "open", None
            session.add(gap)
    activity.log(session, case.id, "caseworker", user.name, f"Edited form field: {field.label}")
    refresh_stage(session, case)
    return field


def record_tenant_submit(session: Session, case: CaseRow) -> None:
    """Tenant ticked both boxes on the intake page. Attestation only (files arrive in M2)."""
    case.tenant_attested_genuine = True
    case.tenant_attested_at = datetime.now(UTC)
    activity.log(session, case.id, "tenant", case.tenant_name,
                 "Confirmed the files are genuine and agreed to share them")
    sync_attestation_gap(session, case)
    refresh_stage(session, case, waiting_on_tenant=False)


def sync_attestation_gap(session: Session, case: CaseRow) -> int:
    """Keep "The tenant must confirm their files" in step with `case.tenant_attested_genuine`:
    resolved when the tenant confirms, open again when new tenant files clear the confirmation.
    A rerun cannot do either, since the gaps step skips a title the case already has. A dismissed
    gap stays dismissed. Returns how many gaps changed. Caller commits."""
    attested = case.tenant_attested_genuine
    changed = 0
    for gap in session.exec(select(GapRow).where(GapRow.case_id == case.id)).all():
        if gap.title.casefold() != UNATTESTED_TITLE.casefold():
            continue
        if attested and gap.status in OPEN:
            gap.status, gap.resolution_note = "resolved", "The tenant confirmed on the intake page"
        elif not attested and gap.status == "resolved":
            gap.status, gap.resolution_note = "open", None
        else:
            continue
        session.add(gap)
        changed += 1
    return changed
