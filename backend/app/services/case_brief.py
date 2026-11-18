"""Case brief: one PDF for the caseworker after the agent has run. Case summary, the timeline,
an index of the uploaded evidence, and the gaps left to review.

Every statement points at the uploaded file it came from (label and locator). A statement no
upload supports says NEEDS CASEWORKER VERIFICATION instead. A timeline source is checked again
against the stored text of the file when there is any; a fact about the tenant (name, unit) is
only called supported when its words are found in an upload. Facts the clinic or the caseworker
entered are labelled as such. `caseworker_note` is never read here, so the brief can be shared
without leaking an internal note. Nothing is stored and nothing is changed."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from fpdf import FPDF
from fpdf.enums import WrapMode
from sqlmodel import Session, col, select

from app.models import (
    CaseRow,
    EvidencePageRow,
    EvidenceRow,
    GapRow,
    LegalSourceRow,
    RunRow,
    TimelineEventRow,
)
from app.services.mappers import OPEN_GAP, has_completed_run
from app.services.package import HINDI_NOTE, NOT_ANALYSED, new_pdf, pdf_text
from app.services.verifier import normalize, verify_citation

NEEDS = "NEEDS CASEWORKER VERIFICATION"
KIND_ORDER = ["lease", "letter", "messages", "receipt", "photo", "document", "other"]
PENDING_NOTE = "Not yet reviewed by a caseworker"
REVIEW = {"accepted": "Accepted", "edited": "Edited", "pending": PENDING_NOTE}


def _write(pdf: FPDF, text: str, *, bold: bool = False, size: int = 10, gap: float = 1.5) -> None:
    """One paragraph. Words wrap at spaces; text with Chinese (no spaces to break on) wraps at
    any character."""
    cjk = any("\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef" for ch in text)
    pdf.set_font("NotoSans", "B" if bold else "", size)
    pdf.multi_cell(0, size * 0.5 + 1.5, pdf_text(pdf, text), new_x="LMARGIN", new_y="NEXT",
                   wrapmode=WrapMode.CHAR if cjk else WrapMode.WORD)
    pdf.ln(gap)


def blockers(session: Session, case: CaseRow) -> list[str]:
    """Why a brief cannot be made yet. Empty means it can."""
    return [] if has_completed_run(session, case.id) else [NOT_ANALYSED]


@dataclass
class Doc:
    """The brief as it is built: (text, style) blocks, and every line that needs verification
    (listed again at the end, and counted at the top)."""

    blocks: list[tuple[str, str]] = field(default_factory=list)
    flagged: list[str] = field(default_factory=list)

    def add(self, text: str, style: str = "p") -> None:
        self.blocks.append((text, style))

    def flag(self, what: str) -> str:
        self.flagged.append(what)
        return f"{NEEDS}: {what}"


def _kind(ev: EvidenceRow) -> str:
    return "document" if ev.kind == "other" and ev.mime == "application/pdf" else ev.kind


def _pages(session: Session, case_id: str) -> dict[str, dict[str, str]]:
    rows = session.exec(select(EvidencePageRow).where(EvidencePageRow.case_id == case_id)).all()
    pages: dict[str, dict[str, str]] = defaultdict(dict)
    for row in rows:
        pages[row.evidence_id][row.locator] = row.text
    return pages


def _find_in_uploads(
    value: str, evidence: list[EvidenceRow], pages: dict[str, dict[str, str]]
) -> str | None:
    """"E-02 message 1" for the first upload page that holds `value` word for word, else None."""
    needle = normalize(value)
    if not needle:
        return None
    for ev in evidence:
        for locator, text in pages.get(ev.id, {}).items():
            if needle in normalize(text):
                return f"{ev.label} {locator}"
    return None


def _when(dt: datetime) -> str:
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).strftime("%Y-%m-%d %H:%M UTC")


def _summary(doc: Doc, case: CaseRow, evidence, pages, events, gaps) -> None:
    doc.add("1. Case summary", "h2")

    def fact(label: str, value: str, found: str | None) -> None:
        source = f"found in {found}" if found else doc.flag(f"{label} is not in any uploaded file")
        doc.add(f"{label}: {value}  [{source}]")

    fact("Tenant", case.tenant_name, _find_in_uploads(case.tenant_name, evidence, pages))
    fact("Rental unit", case.unit, _find_in_uploads(case.unit, evidence, pages))
    doc.add(f"Case: {case.title}  [case record, entered by the clinic]")
    doc.add(f"Tenant's language: {case.tenant_language}  [case record]")
    if case.hearing_date:
        doc.add(f"Hearing date: {case.hearing_date}  [entered by a caseworker, not from an upload]")
    else:
        doc.add(f"Hearing date: {doc.flag('no hearing date has been entered')}")
    attested = (
        f"yes, {_when(case.tenant_attested_at)}" if case.tenant_attested_genuine
        and case.tenant_attested_at else "no"
    )
    doc.add(f"Tenant confirmed the files are genuine: {attested}  [intake form]")
    doc.add(
        f"{len(evidence)} uploaded files, {len(events)} timeline events, "
        f"{len(gaps)} open items for review.",
    )


def _sources(doc: Doc, event, by_id, pages) -> bool:
    """Write an event's sources; True when at least one uploaded file supports it."""
    supported = False
    for src in event.sources:
        ev = by_id.get(src.get("evidence_id"))
        if ev is None:
            doc.add("   - a source that is not an uploaded file of this case", "small")
            continue
        locator, excerpt = str(src.get("locator") or ""), str(src.get("excerpt") or "")
        page_map = pages.get(ev.id, {})
        if page_map and excerpt:
            ok = verify_citation(excerpt, locator, page_map).ok
            state = "checked against the stored text" if ok else "NOT found in the stored text"
            supported = supported or ok
        else:
            state = "stored excerpt, no stored text to check it against"
            supported = True
        quote = f" “{excerpt}”" if excerpt else ""
        doc.add(f"   - {ev.label} ({ev.filename}), {locator}:{quote}  [{state}]", "small")
    return supported


def _timeline(doc: Doc, events, by_id, pages) -> None:
    doc.add("2. Timeline", "h2")
    if not events:
        doc.add("No timeline events yet.")
        return
    for event in events:
        date = event.date or {}
        shown = date.get("value") or "date unknown"
        basis = date.get("basis", "")
        doc.add(f"{shown}  {event.title}", "b")
        doc.add(
            f"{REVIEW.get(event.review_status, event.review_status)}. Strength: "
            f"{event.strength}. Date taken from: {basis or 'not stated'}.",
            "small",
        )
        doc.add(event.description)
        for flag in event.flags:
            doc.add(f"Flag: {flag.get('message', '')}", "small")
        if not _sources(doc, event, by_id, pages):
            doc.add(doc.flag(f"timeline event “{event.title}” has no supporting upload"), "b")


def _evidence(doc: Doc, evidence, events) -> None:
    doc.add("3. Evidence index", "h2")
    cited: dict[str, int] = defaultdict(int)
    for event in events:
        for src in event.sources:
            cited[src.get("evidence_id")] += 1

    def order(ev: EvidenceRow) -> tuple[int, str]:
        kind = _kind(ev)
        meta = (ev.metadata_ or {}).get("captured_at") or ""
        return (KIND_ORDER.index(kind) if kind in KIND_ORDER else len(KIND_ORDER),
                meta or ev.received_at.isoformat())

    if not evidence:
        doc.add("No files uploaded.")
    for ev in sorted(evidence, key=order):
        meta = ev.metadata_ or {}
        captured = f", captured {meta['captured_at']}" if meta.get("captured_at") else ""
        used = cited.get(ev.id, 0)
        doc.add(
            f"{ev.label}  {ev.filename}  ({_kind(ev)}, uploaded by {ev.uploaded_by}, received "
            f"{_when(ev.received_at)}{captured})",
            "b",
        )
        doc.add(
            f"SHA-256 {ev.sha256}. Cited by {used} timeline event{'s' if used != 1 else ''}"
            + ("" if ev.processing == "done" else f". {NEEDS}: this file was not processed "
               f"({ev.processing})"),
            "small",
        )
        if ev.processing != "done":
            doc.flagged.append(f"{ev.label} was not processed ({ev.processing})")


def _gaps(doc: Doc, gaps, sources: dict[str, str], latest: RunRow | None) -> None:
    doc.add("4. Gaps and items for caseworker review", "h2")
    step = next((s for s in (latest.steps if latest else []) if s.get("key") == "gaps"), None)
    if step is None or step.get("status") != "done":
        doc.add(doc.flag("the gaps step has not completed for this case, so missing items have "
                         "not been checked"), "b")
    if not gaps:
        doc.add("No open items are recorded.")
    for gap in gaps:
        state = "waiting for the tenant" if gap.status == "waiting_tenant" else "open"
        doc.add(f"[{gap.severity}] {gap.title}  ({state})", "b")
        doc.add(gap.why)
        doc.add(f"What to do: {gap.how_to_fix}", "small")
        basis = sources.get(gap.source_id or "")
        doc.add(
            "Why it is listed: an upload is missing or does not support it"
            + (f". Rule source: {basis}." if basis else "."),
            "small",
        )
        if gap.tenant_request:
            waiting = gap.status == "waiting_tenant"
            sent = "sent to the tenant" if waiting else "waiting for your approval"
            doc.add(f"A drafted request to the tenant is {sent}.", "small")


def build_brief(session: Session, case: CaseRow, *, now: datetime | None = None) -> bytes:
    """The PDF. The caller has checked `blockers` and that the case is the signed-in org's."""
    evidence = list(session.exec(
        select(EvidenceRow)
        .where(EvidenceRow.case_id == case.id)
        .order_by(col(EvidenceRow.position))
    ).all())
    pages = _pages(session, case.id)
    by_id = {ev.id: ev for ev in evidence}
    events = sorted(
        (e for e in session.exec(
            select(TimelineEventRow).where(TimelineEventRow.case_id == case.id)).all()
         if e.review_status != "rejected"),
        key=lambda e: (e.sort_key or "9", e.title),
    )
    gaps = sorted(
        (g for g in session.exec(select(GapRow).where(GapRow.case_id == case.id)).all()
         if g.status in OPEN_GAP),
        key=lambda g: (g.severity != "blocking", g.position),
    )
    sources = {s.id: s.title for s in session.exec(
        select(LegalSourceRow).where(LegalSourceRow.case_id == case.id)).all()}
    latest = session.exec(
        select(RunRow).where(RunRow.case_id == case.id).order_by(col(RunRow.started_at).desc())
    ).first()

    body = Doc()
    _summary(body, case, evidence, pages, events, gaps)
    if latest is not None and latest.stale:
        body.add(body.flag("files were added after the last analysis, so this brief may be "
                           "missing them"), "b")
    _timeline(body, events, by_id, pages)
    _evidence(body, evidence, events)
    _gaps(body, gaps, sources, latest)

    pdf = new_pdf(case.tenant_language)
    pdf.set_title(f"Case brief {case.id}")
    stamp = _when(now or datetime.now(UTC))
    _write(pdf, "Case brief for caseworker review", bold=True, size=16, gap=2)
    _write(pdf, f"{case.title} ({case.unit})  ·  case {case.id}  ·  prepared {stamp}", size=9)
    _write(
        pdf,
        "Built only from the uploaded files and the case record. Every statement names the file it "
        "came from; a statement no upload supports says NEEDS CASEWORKER VERIFICATION. This is "
        "information for the caseworker, not legal advice. Internal caseworker notes are not "
        "included.",
        size=9, gap=3,
    )
    if case.tenant_language == "hi":
        _write(pdf, HINDI_NOTE, size=9, gap=3)
    count = len(body.flagged)
    _write(pdf, f"{count} item{'s' if count != 1 else ''} need caseworker verification.", bold=True)
    styles = {"h2": dict(bold=True, size=13, gap=2), "b": dict(bold=True), "small": dict(size=9),
              "p": dict()}
    for text, style in body.blocks:
        _write(pdf, text, **styles[style])  # type: ignore[arg-type]
    _write(pdf, "5. Verification checklist", bold=True, size=13, gap=2)
    if not body.flagged:
        _write(pdf, "Nothing is flagged.")
    for item in body.flagged:
        _write(pdf, f"[ ] {item}")
    return bytes(pdf.output())


def brief_filename(case: CaseRow) -> str:
    return f"tenantproof-case-brief-{case.id}.pdf"

