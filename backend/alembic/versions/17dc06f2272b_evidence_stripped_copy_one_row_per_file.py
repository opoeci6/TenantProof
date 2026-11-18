"""evidence stripped copy, one row per file

Revision ID: 17dc06f2272b
Revises: 971486e2e192
Create Date: 2026-11-12 17:23:33.283256

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = '17dc06f2272b'
down_revision: Union[str, Sequence[str], None] = '971486e2e192'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Postgres's default name for an unnamed UNIQUE (case_id, sha256), so the model needs no name.
UNIQUE_NAME = "evidence_case_id_sha256_key"


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('evidence', sa.Column('stripped_sha256', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
    op.create_unique_constraint(UNIQUE_NAME, 'evidence', ['case_id', 'sha256'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint(UNIQUE_NAME, 'evidence', type_='unique')
    op.drop_column('evidence', 'stripped_sha256')
