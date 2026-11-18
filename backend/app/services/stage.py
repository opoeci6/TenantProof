"""Recompute a case's stage from its rows after any change."""

from datetime import UTC, datetime

from sqlmodel import Session, select

from app.models import CaseRow, FormFieldRow, GapRow
from app.services.gating import compute_stage
from app.services.mappers import case_counts


def refresh_stage(
    session: Session, case: CaseRow, *, waiting_on_tenant: bool | None = None
) -> None:
    """Set case.stage. `waiting_on_tenant`: True when a request was just sent, False when the
    tenant just answered; None keeps waiting_tenant only while a request is still outstanding.
    Caller commits."""
    session.flush()
    if waiting_on_tenant is None:
        waiting_on_tenant = case.stage == "waiting_tenant" and session.exec(
            select(GapRow).where(GapRow.case_id == case.id, GapRow.status == "waiting_tenant")
        ).first() is not None
    blocking, pending = case_counts(session, case.id)
    fields = session.exec(select(FormFieldRow).where(FormFieldRow.case_id == case.id)).all()
    blocking += sum(1 for f in fields if f.required and not f.value)  # same rule as package gating
    case.stage = compute_stage(
        case.stage, open_blocking=blocking, pending_review=pending,
        waiting_on_tenant=waiting_on_tenant,
    )
    case.updated_at = datetime.now(UTC)
    session.add(case)
