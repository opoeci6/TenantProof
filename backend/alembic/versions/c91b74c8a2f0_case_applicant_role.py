"""Store the Form J applicant role, defaulting existing cases to tenant.

Revision ID: c91b74c8a2f0
Revises: 8d2e5b1c9a47
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c91b74c8a2f0"
down_revision: str | Sequence[str] | None = "8d2e5b1c9a47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CONSTRAINT = "ck_tenant_case_applicant_role"


def upgrade() -> None:
    op.add_column(
        "tenant_case",
        sa.Column("applicant_role", sa.String(8), nullable=False, server_default="tenant"),
    )
    op.create_check_constraint(_CONSTRAINT, "tenant_case", "applicant_role IN ('tenant', 'landlord')")


def downgrade() -> None:
    op.drop_constraint(_CONSTRAINT, "tenant_case", type_="check")
    op.drop_column("tenant_case", "applicant_role")
