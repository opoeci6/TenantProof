"""Store caseworkers' choices about personal details sent to the model (issue #60).

Revision ID: 5f1d3c7a9b20
Revises: c91b74c8a2f0
"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

revision: str = "5f1d3c7a9b20"
down_revision: str | Sequence[str] | None = "c91b74c8a2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "privacy_term",
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("case_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("value", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("value_key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("action", sa.String(4), nullable=False),
        sa.Column("role", sa.String(8), nullable=False),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["case_id"], ["tenant_case.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("case_id", "value_key"),
        sa.CheckConstraint("action IN ('hide', 'show')", name="ck_privacy_term_action"),
        sa.CheckConstraint(
            "role IN ('tenant', 'landlord', 'person', 'address', 'other')",
            name="ck_privacy_term_role",
        ),
    )
    op.create_index("ix_privacy_term_case_id", "privacy_term", ["case_id"])


def downgrade() -> None:
    op.drop_index("ix_privacy_term_case_id", table_name="privacy_term")
    op.drop_table("privacy_term")
