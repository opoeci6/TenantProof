"""Remember the key a new-case submission was made with, so a retry cannot duplicate the case
(issue #202).

Revision ID: a7c4e19b2d63
Revises: 5f1d3c7a9b20
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a7c4e19b2d63"
down_revision: str | Sequence[str] | None = "5f1d3c7a9b20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenant_case", sa.Column("idempotency_key", sa.String(length=64), nullable=True)
    )
    # Partial: rows without a key (seeds, tests, cases made before this) never collide.
    op.create_index(
        "uq_tenant_case_org_idempotency_key",
        "tenant_case",
        ["org_id", "idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_tenant_case_org_idempotency_key", table_name="tenant_case")
    op.drop_column("tenant_case", "idempotency_key")
