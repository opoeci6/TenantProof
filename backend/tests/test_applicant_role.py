"""PostgreSQL defaults and constraints for the case's Form J applicant role."""

import pytest
from psycopg.errors import CheckViolation, NotNullViolation
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.models import CaseRow

CASE_ID = "case_demo_001"


def test_applicant_role_defaults_to_tenant_and_persists_landlord(fresh_db, session: Session):
    default = session.connection().execute(
        text("UPDATE tenant_case SET applicant_role = DEFAULT WHERE id = :case_id "
             "RETURNING applicant_role"),
        {"case_id": CASE_ID},
    ).scalar_one()
    assert default == "tenant"

    case = session.get(CaseRow, CASE_ID)
    assert case is not None and case.applicant_role == "tenant"
    case.applicant_role = "landlord"
    session.add(case)
    session.commit()
    session.refresh(case)
    assert case.applicant_role == "landlord"


@pytest.mark.parametrize(("value", "error"), [
    ("lawyer", CheckViolation),
    (None, NotNullViolation),
])
def test_database_rejects_invalid_applicant_roles(session: Session, value, error):
    with pytest.raises(IntegrityError) as raised, session.begin_nested():
        session.connection().execute(
            text("UPDATE tenant_case SET applicant_role = :role WHERE id = :case_id"),
            {"role": value, "case_id": CASE_ID},
        )
    assert isinstance(raised.value.orig, error)
