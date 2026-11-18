"""Where the agent's tools get evidence text and extracted facts.

The tools read through the `EvidenceTextStore` protocol. `SqlTextStore` serves the
`evidence_page` and `evidence_fact` tables (WS1 ingest writes the pages, the extract step writes
the facts); `InMemoryTextStore` is for tests. Only stripped content is ever served: no EXIF,
no GPS, never the original file. Every query filters on `case_id`, so an evidence id from
another case answers with nothing, the same as an id that does not exist."""

from dataclasses import dataclass, field
from typing import Protocol

from sqlmodel import Session, col, select

from app.models import EvidenceFactRow, EvidencePageRow


@dataclass(frozen=True)
class Page:
    locator: str  # "page 1", "message 3": what a SourceRef.locator must match
    text: str


@dataclass(frozen=True)
class Fact:
    evidence_id: str
    locator: str
    text: str  # the extracted fact, copied from the evidence


class EvidenceTextStore(Protocol):
    def pages(self, case_id: str, evidence_id: str) -> list[Page]:
        """The pages of one evidence item of this case, in order. Empty when no text exists."""
        ...

    def facts(self, case_id: str) -> list[Fact]:
        """Every extracted fact of this case."""
        ...


@dataclass
class InMemoryTextStore:
    """Keyed by case, so a lookup for another case's evidence id finds nothing."""

    _pages: dict[tuple[str, str], list[Page]] = field(default_factory=dict)
    _facts: dict[str, list[Fact]] = field(default_factory=dict)

    def add_pages(self, case_id: str, evidence_id: str, pages: list[Page]) -> None:
        self._pages[(case_id, evidence_id)] = pages

    def add_facts(self, case_id: str, facts: list[Fact]) -> None:
        self._facts.setdefault(case_id, []).extend(facts)

    def pages(self, case_id: str, evidence_id: str) -> list[Page]:
        return list(self._pages.get((case_id, evidence_id), []))

    def facts(self, case_id: str) -> list[Fact]:
        return list(self._facts.get(case_id, []))


class SqlTextStore:
    """Reads the `evidence_page` and `evidence_fact` tables through the caller's session.
    Read-only: it never writes. Pages come back in `position` order, facts in evidence
    then `position` order, so the same rows always give the same answer."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def pages(self, case_id: str, evidence_id: str) -> list[Page]:
        rows = self._session.exec(
            select(EvidencePageRow)
            .where(EvidencePageRow.case_id == case_id, EvidencePageRow.evidence_id == evidence_id)
            .order_by(col(EvidencePageRow.position), col(EvidencePageRow.locator))
        ).all()
        return [Page(r.locator, r.text) for r in rows]

    def facts(self, case_id: str) -> list[Fact]:
        rows = self._session.exec(
            select(EvidenceFactRow)
            .where(EvidenceFactRow.case_id == case_id)
            .order_by(col(EvidenceFactRow.evidence_id), col(EvidenceFactRow.position),
                      col(EvidenceFactRow.id))
        ).all()
        return [Fact(r.evidence_id, r.locator, r.text) for r in rows]
