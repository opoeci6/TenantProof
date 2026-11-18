"""tenant answers and cached excerpt translations

Revision ID: 3c9f1a7be204
Revises: 17dc06f2272b
Create Date: 2026-11-18 00:20:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3c9f1a7be204"
down_revision: str | Sequence[str] | None = "17dc06f2272b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("gap", sa.Column("answer", sa.JSON(), nullable=True))
    op.create_table(
        "excerpt_translation",
        sa.Column("key", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("language", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("text_en", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("created_at", sqlmodel.sql.sqltypes.UTCDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("excerpt_translation")
    op.drop_column("gap", "answer")
