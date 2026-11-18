"""case.no_hearing_yet: the caseworker said no hearing is scheduled yet

Revision ID: 8d2e5b1c9a47
Revises: 3c9f1a7be204
Create Date: 2026-11-18 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d2e5b1c9a47"
down_revision: str | Sequence[str] | None = "3c9f1a7be204"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "tenant_case",
        sa.Column("no_hearing_yet", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("tenant_case", "no_hearing_yet")
