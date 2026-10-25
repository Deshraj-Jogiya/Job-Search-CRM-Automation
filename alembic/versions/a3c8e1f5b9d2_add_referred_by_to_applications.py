"""add referred_by to job_applications

Revision ID: a3c8e1f5b9d2
Revises: e2b7c4a9d1f3
Create Date: 2026-10-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c8e1f5b9d2'
down_revision: Union[str, Sequence[str], None] = 'e2b7c4a9d1f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('job_applications')}
    # create_all() at startup may already have added it (same situation as e2b7c4a9d1f3).
    if 'referred_by' not in existing:
        with op.batch_alter_table('job_applications') as batch:
            batch.add_column(sa.Column('referred_by', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('job_applications') as batch:
        batch.drop_column('referred_by')
