"""Row -> contract-shape mappers. Keeps routers thin and shapes in one place."""

from datetime import UTC, date, datetime

from sqlalchemy import ColumnElement, exists, or_
from sqlalchemy import select as sa_select  # sqlmodel.select takes at most four columns
from sqlmodel import Session, col, func, select

from app.models import (
    ActivityRow,
    CaseRow,
    EvidenceRow,
    FormFieldRow,
    GapRow,
    LegalSourceRow,
    RunRow,
    TimelineEventRow,
)
from app.schemas import (
    ActivityEntry,
    Case,
    CaseSummary,
    EvidenceItem,
    FormDraft,
    FormField,
    Gap,
    Intake,
    IntakeRequest,
    IntakeView,
    LegalSource,
    Run,
    TimelineEvent,
)
from app.services.dates import demo_deadline
from app.services.legal.corpus import FORM_J_DRAFT_LABEL
from app.services.narrative import build_narrative
from app.services.priority import PriorityInput, compute_priority, sort_key

OPEN_GAP = ("open", "waiting_tenant")
COMPLETED_RUN = ("done", "needs_review")  # a paused run is needs_review with no finished_at
REVIEWED = ("accepted", "edited")


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def iso_req(dt: datetime) -> str:
    return iso(dt) or ""


def to_intake(case: CaseRow, url: str | None = None, qr_svg: str | None = None) -> Intake:
    """`url` and `qr_svg` only for the response that issues the link; None everywhere else."""
    return Intake.model_validate(
        {
            "url": url,
            "qr_svg": qr_svg,
            "status": case.intake_status,
            "sent_at": iso(case.intake_sent_at),
            "last_upload_at": iso(case.intake_last_upload_at),
            "tenant_attested_genuine": case.tenant_attested_genuine,
            "tenant_attested_at": iso(case.tenant_attested_at),
        }
    )


def _blocking_count() -> ColumnElement[int]:
    """Open blocking gaps of the enclosing case, as a correlated scalar subquery."""
    return (
        select(func.count())
        .select_from(GapRow)
        .where(
            GapRow.case_id == CaseRow.id,
            GapRow.severity == "blocking",
            col(GapRow.status).in_(OPEN_GAP),
        )
        .scalar_subquery()
    )


def _pending_count() -> ColumnElement[int]:
    """Timeline events of the enclosing case still waiting for review."""
    return (
        select(func.count())
        .select_from(TimelineEventRow)
        .where(TimelineEventRow.case_id == CaseRow.id, TimelineEventRow.review_status == "pending")
        .scalar_subquery()
    )


def _analysed() -> ColumnElement[bool]:
    """True when the enclosing case has a completed run or a reviewed (accepted or edited)
    event. `has_completed_run` and `reviewed_event_count` below are the same two rules."""
    run = exists().where(
        col(RunRow.case_id) == CaseRow.id, col(RunRow.status).in_(COMPLETED_RUN),
        col(RunRow.finished_at).is_not(None),
    )
    event = exists().where(
        col(TimelineEventRow.case_id) == CaseRow.id,
        col(TimelineEventRow.review_status).in_(REVIEWED),
    )
    return or_(run, event)


def has_completed_run(session: Session, case_id: str) -> bool:
    return session.exec(
        select(RunRow.id).where(
            RunRow.case_id == case_id, col(RunRow.status).in_(COMPLETED_RUN),
            col(RunRow.finished_at).is_not(None),
        ).limit(1)
    ).first() is not None


def reviewed_event_count(session: Session, case_id: str) -> int:
    """Events a caseworker accepted or edited: the ones that go into the form."""
    return int(session.exec(
        select(func.count()).select_from(TimelineEventRow).where(
            TimelineEventRow.case_id == case_id, col(TimelineEventRow.review_status).in_(REVIEWED)
        )
    ).one())


def _last_activity_at() -> ColumnElement[datetime]:
    return (
        select(func.max(ActivityRow.at))
        .where(ActivityRow.case_id == CaseRow.id)
        .scalar_subquery()
    )


def list_case_rows(
    session: Session, org_id: str
) -> list[tuple[CaseRow, int, int, datetime | None, bool]]:
    """The org's cases with their counts, last activity time and `analysed`, in one query."""
    rows = session.execute(
        sa_select(
            CaseRow, _blocking_count(), _pending_count(), _last_activity_at(), _analysed()
        ).where(col(CaseRow.org_id) == org_id)
    ).all()
    return [
        (case, int(blocking), int(pending), last, bool(analysed))
        for case, blocking, pending, last, analysed in rows
    ]


def case_counts(session: Session, case_id: str) -> tuple[int, int]:
    query = select(_blocking_count(), _pending_count()).where(CaseRow.id == case_id)
    blocking, pending = session.exec(query).one()
    return int(blocking), int(pending)


def to_summary(
    case: CaseRow, blocking: int, pending: int, last_at: datetime | None, today: date,
    analysed: bool | None = None,
) -> tuple[CaseSummary, tuple[int, int, int, int]]:
    deadline = demo_deadline(case.next_deadline, today) if case.next_deadline else None
    result = compute_priority(
        PriorityInput(
            stage=case.stage,
            next_deadline_date=date.fromisoformat(deadline["date"]) if deadline else None,
            open_blocking=blocking,
            pending_review=pending,
            intake_sent=case.intake_status != "not_sent",
        ),
        today,
    )
    summary = CaseSummary.model_validate(
        {
            "id": case.id,
            "tenant_name": case.tenant_name,
            "title": case.title,
            "unit": case.unit,
            "dispute_type": case.dispute_type,
            "stage": case.stage,
            "next_deadline": deadline,
            "open_blocking": blocking,
            "pending_review": pending,
            "tenant_language": case.tenant_language,
            "priority": result.priority,
            "priority_reason": result.reason,
            "updated_at": iso_req(last_at or case.updated_at),
            "has_demo_data": case.has_demo_data,
            "analysed": analysed,
        }
    )
    return summary, sort_key(result.priority, result.days_to_deadline, blocking, pending)


def to_case(case: CaseRow, assignee_name: str) -> Case:
    return Case.model_validate(
        {
            "id": case.id,
            "dispute_type": case.dispute_type,
            "title": case.title,
            "unit": case.unit,
            "assigned_to": {"id": case.assigned_to_id, "name": assignee_name},
            "tenant": {"name": case.tenant_name, "preferred_language": case.tenant_language},
            "intake": to_intake(case).model_dump(),
            "created_at": iso_req(case.created_at),
            "synthetic": case.synthetic,
        }
    )


def to_evidence(row: EvidenceRow) -> EvidenceItem:
    return EvidenceItem.model_validate(
        {
            "id": row.id,
            "label": row.label,
            "filename": row.filename,
            "kind": row.kind,
            "mime": row.mime,
            "size_bytes": row.size_bytes,
            "sha256": row.sha256,
            "received_at": iso_req(row.received_at),
            "uploaded_by": row.uploaded_by,
            "format_check": row.format_check,
            "metadata": row.metadata_,
            "metadata_stripped_for_ai": row.metadata_stripped_for_ai,
            "processing": row.processing,
            "error": row.error,
            # #97: GET /cases/{id}/evidence/{evidence_id}/preview serves this copy
            "has_preview": row.stripped_sha256 is not None,
        }
    )


def to_run(row: RunRow) -> Run:
    return Run.model_validate(
        {
            "id": row.id,
            "case_id": row.case_id,
            "status": row.status,
            "started_at": iso_req(row.started_at),
            "finished_at": iso(row.finished_at),
            "steps": row.steps,
            "stale": row.stale,
            "usage": row.usage,
        }
    )


def to_event(row: TimelineEventRow) -> TimelineEvent:
    return TimelineEvent.model_validate(
        {
            "id": row.id,
            "date": row.date,
            "title": row.title,
            "description": row.description,
            "actor": row.actor,
            "sources": row.sources,
            "strength": row.strength,
            "strength_reasons": row.strength_reasons,
            "flags": row.flags,
            "review_status": row.review_status,
            "caseworker_note": row.caseworker_note,
            "reviewed_by": row.reviewed_by,
        }
    )


def to_gap(row: GapRow) -> Gap:
    return Gap.model_validate(
        {
            "id": row.id,
            "severity": row.severity,
            "title": row.title,
            "why": row.why,
            "how_to_fix": row.how_to_fix,
            "source_id": row.source_id,
            "input": row.input,
            "tenant_request": row.tenant_request,
            "answer": row.answer,
            "status": row.status,
            "resolution_note": row.resolution_note,
        }
    )


def to_source(row: LegalSourceRow) -> LegalSource:
    return LegalSource.model_validate(
        {
            "id": row.id,
            "title": row.title,
            "kind": row.kind,
            "url": row.url,
            "last_verified_at": iso(row.last_verified_at),
            "verify_status": row.verify_status,
            "used_for": row.used_for,
        }
    )


def to_field(row: FormFieldRow) -> FormField:
    return FormField.model_validate(
        {
            "key": row.key,
            "label": row.label,
            "value": row.value,
            "required": row.required,
            "status": row.status,
            "sources": row.sources,
            "note": row.note,
        }
    )


def to_activity(row: ActivityRow) -> ActivityEntry:
    return ActivityEntry.model_validate(
        {
            "at": iso_req(row.at),
            "actor": row.actor,
            "actor_name": row.actor_name,
            "action": row.action,
        }
    )


def build_form_draft(session: Session, case: CaseRow) -> FormDraft:
    fields = session.exec(
        select(FormFieldRow)
        .where(FormFieldRow.case_id == case.id)
        .order_by(col(FormFieldRow.position))
    ).all()
    events = session.exec(
        select(TimelineEventRow)
        .where(TimelineEventRow.case_id == case.id)
        .order_by(TimelineEventRow.sort_key)
    ).all()
    labels = {
        e.id: e.label
        for e in session.exec(select(EvidenceRow).where(EvidenceRow.case_id == case.id)).all()
    }
    narrative = build_narrative(
        [
            {
                "id": e.id,
                "date": e.date,
                "description": e.description,
                "sources": e.sources,
                "review_status": e.review_status,
            }
            for e in events
        ],
        labels,
    )
    return FormDraft.model_validate(
        {
            "form_name": FORM_J_DRAFT_LABEL if case.form_name else "",  # also for old rows
            "disclaimer": case.form_disclaimer,
            "fields": [to_field(f).model_dump() for f in fields],
            "narrative": narrative,
        }
    )


def answered_since_sent(gap: GapRow) -> bool:
    """The tenant answered this request after it was last sent (demo test bug 8). A request sent
    again after an answer counts as open; one sent before `sent_at` was stored counts as answered
    once it has an answer."""
    if not gap.answer:
        return False
    sent_at = (gap.tenant_request or {}).get("sent_at")
    if not sent_at:
        return True
    try:
        return datetime.fromisoformat(gap.answer["received_at"]) >= datetime.fromisoformat(sent_at)
    except (KeyError, TypeError, ValueError):
        return True


def to_intake_view(session: Session, case: CaseRow, org_name: str) -> IntakeView:
    gaps = session.exec(
        select(GapRow).where(GapRow.case_id == case.id, GapRow.status == "waiting_tenant")
    ).all()
    return IntakeView(
        org_name=org_name,
        tenant_first_name=case.tenant_name.split(" ")[0],
        unit=case.unit,
        language=case.tenant_language,
        requests=[
            IntakeRequest(
                id=g.id, message=g.tenant_request["message"],
                message_en=g.tenant_request.get("message_en") or g.tenant_request["message"],
            )
            for g in gaps
            # An answered request leaves the tenant's page; the caseworker still sees the answer.
            if g.tenant_request and not answered_since_sent(g)
        ],
    )
