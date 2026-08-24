"""add recruitee and personio slugs to companies

Revision ID: 6328da1202c8
Revises: 1e948a01be48
Create Date: 2026-08-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6328da1202c8'
down_revision: Union[str, Sequence[str], None] = '1e948a01be48'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('companies', sa.Column('recruitee_slug', sa.String(), nullable=True))
    op.add_column('companies', sa.Column('personio_slug', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('companies', 'personio_slug')
    op.drop_column('companies', 'recruitee_slug')
