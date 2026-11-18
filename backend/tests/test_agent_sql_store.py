"""SqlTextStore: the agent's text store backed by evidence_page and evidence_fact, including
that it never serves another case's rows and that the tools work through it."""

import pytest
from sqlmodel import Session, col, delete, select

from app.models import CaseRow, EvidenceFactRow, EvidencePageRow
from app.services.agent.evidence_text import EvidenceTextStore, Fact, Page, SqlTextStore
from tests.agent_support import CASE, LEASE, MSG1, call, make_ctx


@pytest.fixture
def rows(fresh_db: None, session: Session):
    """Pages and facts for the seeded case, removed again afterwards."""
    session.add(EvidencePageRow(evidence_id="ev_04", locator="message 2", case_id=CASE,
                                position=1, text="second"))
    session.add(EvidencePageRow(evidence_id="ev_04", locator="message 1", case_id=CASE,
                                position=0, text=MSG1))
    session.add(EvidencePageRow(evidence_id="ev_01", locator="page 1", case_id=CASE,
                                position=0, text=LEASE))
    session.add(EvidenceFactRow(id="f2", case_id=CASE, evidence_id="ev_04", sha256="a" * 64,
                                prompt_version="v1", locator="message 2", text="later", position=1))
    session.add(EvidenceFactRow(id="f1", case_id=CASE, evidence_id="ev_04", sha256="a" * 64,
                                prompt_version="v1", locator="message 1", text="water leak",
                                position=0))
    session.commit()
    yield
    session.exec(delete(EvidenceFactRow))  # type: ignore[call-overload]
    session.exec(delete(EvidencePageRow))  # type: ignore[call-overload]
    session.commit()


def other_case_id(session: Session) -> str:
    return session.exec(select(CaseRow.id).where(CaseRow.id != CASE)).first() or "case_none"


def test_it_satisfies_the_protocol(session: Session):
    store: EvidenceTextStore = SqlTextStore(session)
    assert store.pages(CASE, "ev_04") == []
    assert store.facts(CASE) == []


def test_pages_come_back_in_position_order(rows, session: Session):
    assert SqlTextStore(session).pages(CASE, "ev_04") == [
        Page("message 1", MSG1), Page("message 2", "second")]


def test_facts_are_ordered_and_scoped_to_the_case(rows, session: Session):
    store = SqlTextStore(session)
    assert store.facts(CASE) == [Fact("ev_04", "message 1", "water leak"),
                                 Fact("ev_04", "message 2", "later")]
    assert store.facts(other_case_id(session)) == []


def test_another_cases_id_pair_finds_nothing(rows, session: Session):
    """The evidence id exists and has pages, but not under this case id."""
    store = SqlTextStore(session)
    assert store.pages(other_case_id(session), "ev_04") == []
    assert store.pages(CASE, "ev_missing") == []


def test_deleting_the_evidence_removes_its_pages_and_facts(rows, session: Session):
    from app.models import EvidenceRow

    session.exec(delete(EvidenceRow).where(col(EvidenceRow.id) == "ev_04"))  # type: ignore[call-overload]
    session.commit()
    store = SqlTextStore(session)
    assert store.pages(CASE, "ev_04") == [] and store.facts(CASE) == []
    assert store.pages(CASE, "ev_01") == [Page("page 1", LEASE)]


def test_read_evidence_and_search_facts_serve_the_tables(rows, session: Session):
    ctx = make_ctx(session, SqlTextStore(session))  # type: ignore[arg-type]
    first = call("read_evidence", ctx, {"evidence_id": "ev_04"})
    assert first.locator == "message 1" and MSG1 in first.block.content
    second = call("read_evidence", ctx, {"evidence_id": "ev_04", "page": 2})
    assert second.locator == "message 2" and "second" in second.block.content
    found = call("search_facts", ctx, {"query": "water"})
    assert "water leak" in str(found)


def test_read_evidence_for_another_cases_id_is_not_found(rows, session: Session):
    ctx = make_ctx(session, SqlTextStore(session), case_id=other_case_id(session))  # type: ignore[arg-type]
    result = call("read_evidence", ctx, {"evidence_id": "ev_04"})
    assert "not_found" in str(result).lower() or "not found" in str(result).lower()
