"""The gaps step's database side (WS4, step 5a and the start of 5b).

`prepare` is what runs before the gaps agent: it gives a case the form it drafts against (only
when the case has no form at all), writes the gaps the deterministic checks found, and returns
what the agent needs to be told — the open gaps and the tenant's language. The agent then reads
the evidence and adds what only reading can find; it never re-creates a gap.

Nothing here resolves, dismisses or sends anything: a gap lands `open`, and a draft on it waits
for a caseworker. That is design rule 7 and the reason the tools are draft-only.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlmodel import Session, col, select

from app.models import (
    ActivityRow,
    CaseRow,
    EvidenceRow,
    FormFieldRow,
    GapRow,
    LegalSourceRow,
    TimelineEventRow,
)
from app.services import activity
from app.services.agent.tools import clean_text
from app.services.form_fields import F2_BY_KEY, covered_legacy_fields, is_required
from app.services.legal import corpus, gap_checks, rules
from app.services.legal.gap_checks import CaseFacts, EventFact, EvidenceFact, FieldFact, GapSpec
from app.services.review import request_sent_line, sync_attestation_gap
from app.services.strength import SourceFacts

ACTOR_NAME = "TenantProof gaps"
EVENT_TITLE_CHARS = 80  # an event title inside a gap title


@dataclass(frozen=True)
class Prepared:
    """What the deterministic part did, and what the agent is told about the case."""

    created: int
    fields_added: int
    open_gaps: tuple[str, ...]  # one line per open gap, for the kickoff
    language: str


def case_facts(session: Session, case: CaseRow) -> CaseFacts:
    """The case as `gap_checks` sees it. The photo facts come from `strength.SourceFacts`, so the
    gaps step and the timeline flags cannot disagree about what "edited" means."""
    rows = session.exec(
        select(FormFieldRow)
        .where(FormFieldRow.case_id == case.id)
        .order_by(col(FormFieldRow.position))
    ).all()
    covered_fields = covered_legacy_fields(rows)
    fields = tuple(
        FieldFact(key=row.key, label=row.label, required=is_required(row, covered_fields),
                  status=row.status)
        for row in rows if row.key not in F2_BY_KEY
    )
    evidence = tuple(
        _evidence_fact(row)
        for row in session.exec(
            select(EvidenceRow)
            .where(EvidenceRow.case_id == case.id)
            .order_by(col(EvidenceRow.position))
        ).all()
    )
    events = tuple(
        # A title the timeline agent wrote from the evidence: cleaned and kept short, since it
        # becomes part of a gap title.
        EventFact(
            title=clean_text(row.title, EVENT_TITLE_CHARS), strength=row.strength,
            rejected=row.review_status == "rejected",
        )  # fmt: skip
        for row in session.exec(
            select(TimelineEventRow)
            .where(TimelineEventRow.case_id == case.id)
            .order_by(col(TimelineEventRow.sort_key), col(TimelineEventRow.id))
        ).all()
    )
    return CaseFacts(
        attested=bool(case.tenant_attested_genuine), fields=fields, evidence=evidence,
        events=events,
    )  # fmt: skip


def _evidence_fact(row: EvidenceRow) -> EvidenceFact:
    meta = row.metadata_ or {}
    facts = SourceFacts(
        evidence_id=row.id, kind=row.kind, uploaded_by=row.uploaded_by,
        captured_at=meta.get("captured_at"), software=meta.get("software"),
    )  # fmt: skip
    return EvidenceFact(
        evidence_id=row.id, label=row.label, kind=row.kind,
        no_metadata=facts.no_metadata, edited=facts.edited,
    )  # fmt: skip


def ensure_form_fields(session: Session, case: CaseRow) -> int:
    """Give a case with no form at all the Form J field list, so the form draft has something to
    fill and the missing-field check has something to check. A case that already has fields is
    left alone: its fields may have been filled or renamed on purpose. Caller commits.

    This edges into the `draft` step's area; the field list lives in the corpus so the two steps
    agree on it."""
    existing = session.exec(select(FormFieldRow).where(FormFieldRow.case_id == case.id)).all()
    if any(field.key not in F2_BY_KEY for field in existing):
        return 0
    form = next(entry for entry in corpus.CORPUS if entry.kind == "form")
    for position, field in enumerate(form.form_fields):
        session.add(FormFieldRow(
            case_id=case.id, key=field.key, position=position, label=field.label,
            required=field.required, status="missing",
        ))  # fmt: skip
    activity.log(
        session, case.id, "agent", ACTOR_NAME,
        f"Added the {len(form.form_fields)} Form J fields to the form",
    )  # fmt: skip
    return len(form.form_fields)


def write_gaps(session: Session, case: CaseRow, specs: list[GapSpec]) -> list[GapRow]:
    """Write the specs the case does not have yet. A gap is skipped when one with the same title
    exists in **any** status (a dismissed gap must not come back) or, for a field gap, when a gap
    already asks for that field — which is how the seeded demo gaps and these checks agree.
    Caller commits."""
    existing = list(session.exec(select(GapRow).where(GapRow.case_id == case.id)).all())
    titles = {gap.title.casefold() for gap in existing}
    fields = {
        gap.input.get("field_key") for gap in existing if gap.input and gap.input.get("field_key")
    }
    sources = rules.source_ids(session, case)
    position = max((gap.position for gap in existing), default=-1)
    added: list[GapRow] = []
    for spec in specs:
        if spec.title.casefold() in titles:
            continue
        if spec.field_key is not None and spec.field_key in fields:
            continue
        position += 1
        gap = GapRow(
            id=f"gap_{uuid.uuid4().hex[:10]}", case_id=case.id, position=position,
            severity=spec.severity, title=spec.title, why=spec.why, how_to_fix=spec.how_to_fix,
            source_id=sources.get(spec.source) if spec.source else None,
            input=(
                {"type": "text", "field_key": spec.field_key, "placeholder": ""}
                if spec.field_key is not None else None
            ),
            status="open",
        )  # fmt: skip
        session.add(gap)
        activity.log(session, case.id, "agent", ACTOR_NAME, f"Added gap “{spec.title}”")
        titles.add(spec.title.casefold())
        if spec.field_key is not None:
            fields.add(spec.field_key)
        added.append(gap)
    return added


def remove_support_gaps(
    session: Session, case: CaseRow, removed: Sequence[TimelineEventRow]
) -> None:
    """`removed` were just deleted (and flushed): their "Support for" gaps asked about something
    no longer on the timeline. A gap stays if another event still needs it, if it is not open, or
    if its request ever went to the tenant. Caller commits."""
    titles = {
        gap_checks.support_title(clean_text(event.title, EVENT_TITLE_CHARS))
        for event in removed
        if event.strength == "needs_support"
    }
    if not titles:
        return
    titles -= {spec.title for spec in gap_checks.specs(case_facts(session, case))}
    rows = session.exec(
        select(GapRow).where(
            GapRow.case_id == case.id, GapRow.status == "open", col(GapRow.title).in_(titles)
        )
    ).all()
    for gap in rows:
        sent = session.exec(
            select(ActivityRow.id).where(
                ActivityRow.case_id == case.id, ActivityRow.action == request_sent_line(gap.title)
            )
        ).first()
        if sent is None:
            session.delete(gap)
            activity.log(session, case.id, "agent", ACTOR_NAME, f"Removed gap “{gap.title}”")


def open_gap_lines(session: Session, case: CaseRow) -> tuple[str, ...]:
    """The open gaps as the agent reads them: the id it passes to draft_tenant_message, the form
    field if any, and whether a draft is waiting for approval. Without the id the agent cannot
    draft on a gap it did not create itself (found on the first live run)."""
    gaps = session.exec(
        select(GapRow).where(GapRow.case_id == case.id).order_by(col(GapRow.position))
    ).all()
    return tuple(
        f"- {gap.id} [{gap.severity}] {clean_text(gap.title, 200)}"
        + (f" (field {key})" if (key := (gap.input or {}).get("field_key")) else "")
        + (" (a draft is already written)" if gap.tenant_request else " (no draft yet)")
        for gap in gaps
        if gap.status == "open"
    )


def prepare(session: Session, case: CaseRow) -> Prepared:
    """Both deterministic jobs, then what the agent is told. Caller commits.

    It gives the case its corpus sources first, so a gap can cite one even when the rules step
    did not run in this order. `ensure_case_sources` is idempotent, so after `rules` it does
    nothing."""
    rules.ensure_case_sources(session, case)
    fields_added = ensure_form_fields(session, case)
    added = write_gaps(session, case, gap_checks.specs(case_facts(session, case)))
    if sync_attestation_gap(session, case):
        # A case analysed before the tenant confirmed: the confirmation item was never closed.
        state = "done" if case.tenant_attested_genuine else "open again"
        activity.log(
            session, case.id, "agent", ACTOR_NAME,
            f"{gap_checks.UNATTESTED_TITLE}: marked {state} to match the tenant's confirmation",
        )  # fmt: skip
    return Prepared(
        created=len(added), fields_added=fields_added,
        open_gaps=open_gap_lines(session, case), language=case.tenant_language,
    )  # fmt: skip


def gap_rows(session: Session, case: CaseRow) -> list[GapRow]:
    return list(
        session.exec(
            select(GapRow).where(GapRow.case_id == case.id).order_by(col(GapRow.position))
        ).all()
    )


def source_row(session: Session, case: CaseRow, source_id: str) -> LegalSourceRow | None:
    """A gap's source, only if it belongs to this case."""
    row = session.get(LegalSourceRow, source_id)
    return row if row is not None and row.case_id == case.id else None
