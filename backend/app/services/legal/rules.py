"""The rules step (WS4, BUILD_PLAN section 5 "rules"). Deterministic: no LLM, no network.

Two jobs, both about the case rather than the evidence:

1. **Give the case the sources the corpus knows about.** A case is seeded with a few rows, an
   empty case has none, and the corpus is the list of official documents the app may cite. This
   inserts what is missing and refreshes what it has, so every case the UI shows carries the same
   set of sources with the same titles.
2. **Set the evidence deadline from the hearing date**, using the number the corpus carries
   rather than a constant of its own. `dates.evidence_deadline` formats the label; the rule says
   how many days before the hearing evidence is due.

Nothing here decides a legal question. The excerpt and its locator come from the corpus, and a
source nobody confirmed stays `unverified`.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime

from sqlmodel import Session, col, select

from app.models import CaseRow, LegalSourceRow
from app.services import activity, dates
from app.services.legal.corpus import CORPUS, EVIDENCE_DAYS_BEFORE_HEARING, CorpusEntry

ACTOR_NAME = "TenantProof rules"
MAX_ID = 64  # the column is unbounded text, but ids are short everywhere else


@dataclass(frozen=True)
class RulesResult:
    """What the step did, for the run page and the activity log."""

    added: int
    total: int
    checked: int  # rows whose text a source check has compared
    deadline: str | None  # ISO date evidence is due, when the hearing date is known
    summary: str

    def tool_call_output(self) -> str:
        return json.dumps(
            {"added": self.added, "total": self.total, "checked": self.checked,
             "evidence_due": self.deadline},
            ensure_ascii=False,
        )  # fmt: skip


def new_row_id(case_id: str, entry: CorpusEntry) -> str:
    """`src_<case>_<entry>`, which stays readable in the UI and in a log line. A pair too long
    for that becomes a digest: ids are global primary keys, so they must stay unique."""
    candidate = f"src_{case_id}_{entry.id}"
    if len(candidate) <= MAX_ID:
        return candidate
    digest = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:24]
    return f"src_{digest}"


def row_for(rows: list[LegalSourceRow], entry: CorpusEntry) -> LegalSourceRow | None:
    """The case's row for an entry: by URL, then by title. The title fallback is what keeps the
    seeded Form J row (`src_03`, stored without a URL) from being duplicated."""
    for row in rows:
        if entry.url is not None and row.url == entry.url:
            return row
    wanted = entry.title.casefold()
    return next((r for r in rows if r.title.casefold() == wanted), None)


def source_ids(session: Session, case: CaseRow) -> dict[str, str]:
    """Corpus entry id -> this case's LegalSourceRow id, for the gaps that cite a source."""
    rows = list(
        session.exec(select(LegalSourceRow).where(LegalSourceRow.case_id == case.id)).all()
    )
    out: dict[str, str] = {}
    for entry in CORPUS:
        row = row_for(rows, entry)
        if row is not None:
            out[entry.id] = row.id
    return out


def _verified_copy(session: Session, entry: CorpusEntry) -> tuple[str, datetime | None]:
    """The verify status and time to start a new row with: copied from the most recently checked
    row that carries the same URL in any case, so a new case does not start from zero. Only for a
    confirmed entry with an excerpt, the only kind the source check compares: anything else is
    `unverified`, so a seeded or stale status is never passed on to a new case."""
    if entry.url is None or not entry.confirmed_by or not entry.excerpt:
        return "unverified", None
    rows = session.exec(
        select(LegalSourceRow)
        .where(LegalSourceRow.url == entry.url)
        .where(col(LegalSourceRow.last_verified_at).is_not(None))
        .order_by(col(LegalSourceRow.last_verified_at).desc())
    ).all()
    if not rows:
        return "unverified", None
    return rows[0].verify_status, rows[0].last_verified_at


def ensure_case_sources(session: Session, case: CaseRow) -> tuple[int, int, int]:
    """Insert the corpus rows this case is missing and refresh the ones it has. Returns
    (added, total, checked). Existing rows keep their id: they are what the case's citations
    already point at. Caller commits."""
    rows = list(
        session.exec(
            select(LegalSourceRow)
            .where(LegalSourceRow.case_id == case.id)
            .order_by(col(LegalSourceRow.position))
        ).all()
    )
    position = max((r.position for r in rows), default=-1)
    added = 0
    for entry in CORPUS:
        row = row_for(rows, entry)
        if row is not None:
            row.title = entry.title
            row.used_for = entry.used_for
            if entry.url is not None and row.url != entry.url:
                # The seed stores Form J without its URL, and a row made before the corpus moved
                # a document to a new address keeps the old one. The current address is what lets
                # the source check reach that row and the UI show where the document is; the row
                # keeps its id, so nothing that cites it breaks.
                row.url = entry.url
            session.add(row)
            continue
        position += 1
        status, verified_at = _verified_copy(session, entry)
        row = LegalSourceRow(
            id=new_row_id(case.id, entry), case_id=case.id, position=position,
            title=entry.title, kind=entry.kind, url=entry.url,
            verify_status=status, last_verified_at=verified_at,
            used_for=entry.used_for,
        )  # fmt: skip
        session.add(row)
        rows.append(row)
        added += 1
    if added:
        activity.log(
            session, case.id, "agent", ACTOR_NAME,
            f"Added {added} official source{'s' if added != 1 else ''} to the case",
        )  # fmt: skip
    checked = sum(1 for r in rows if r.verify_status in ("unchanged", "changed"))
    return added, len(rows), checked


def apply_deadline(session: Session, case: CaseRow) -> str | None:
    """The evidence deadline, from the hearing date and the corpus rule. Returns the ISO date it
    set, or None when the case has no hearing date yet (the timeline step asks for one)."""
    if case.hearing_date is None:
        return None
    hearing = date.fromisoformat(case.hearing_date)
    due = dates.evidence_deadline(hearing, EVIDENCE_DAYS_BEFORE_HEARING)
    if case.next_deadline != due:
        case.next_deadline = due
        session.add(case)
        activity.log(
            session, case.id, "agent", ACTOR_NAME,
            f"Set the must-be-received-by date to {due['date']} "
            f"({EVIDENCE_DAYS_BEFORE_HEARING} days before the hearing on {hearing.isoformat()})",
        )  # fmt: skip
    return due["date"]


def run(session: Session, case: CaseRow) -> RulesResult:
    """Both jobs, and the summary the run page shows. Caller commits."""
    added, total, checked = ensure_case_sources(session, case)
    due = apply_deadline(session, case)
    sources = f"{total} source{'s' if total != 1 else ''} ({checked} checked)"
    summary = f"{sources}, must be received by {due}" if due else f"{sources}, no hearing date yet"
    return RulesResult(added=added, total=total, checked=checked, deadline=due, summary=summary)
