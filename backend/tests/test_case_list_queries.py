"""GET /api/cases must not run more queries as the caseload grows (it used to make 3 per case)."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import event
from sqlmodel import Session, select

from app.db import get_engine
from app.models import CaseRow

# Session + user lookups, then the caseload query. A fixed ceiling, whatever the number of cases.
MAX_QUERIES = 6


@contextmanager
def count_queries() -> Iterator[list[str]]:
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        statements.append(statement)

    engine = get_engine()
    event.listen(engine, "before_cursor_execute", record)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", record)


def _list_cases_query_count(client) -> tuple[int, int]:
    with count_queries() as statements:
        r = client.get("/api/cases")
    assert r.status_code == 200
    return len(statements), len(r.json())


def _add_cases(session: Session, n: int) -> None:
    base = session.exec(select(CaseRow).where(CaseRow.org_id == "org_demo")).first()
    assert base is not None
    for i in range(n):
        data = {**base.model_dump(), "id": f"case_load_{i}", "title": f"Load case {i}"}
        session.add(CaseRow(**data))
    session.commit()


def test_case_list_query_count_does_not_grow_with_caseload(fresh_db, client, session) -> None:
    before_queries, before_cases = _list_cases_query_count(client)
    _add_cases(session, 10)
    after_queries, after_cases = _list_cases_query_count(client)

    assert after_cases == before_cases + 10
    assert after_queries == before_queries
    assert after_queries <= MAX_QUERIES
