"""add company research columns to companies

Revision ID: b7d2f4a6c8e1
Revises: a3c8e1f5b9d2
Create Date: 2026-10-25 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d2f4a6c8e1'
down_revision: Union[str, Sequence[str], None] = 'a3c8e1f5b9d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('website_domain', sa.String()),
    ('research_brief', sa.Text()),
    ('researched_at', sa.DateTime()),
)


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('companies')}
    # create_all() at startup may already have added them (same situation as e2b7c4a9d1f3).
    with op.batch_alter_table('companies') as batch:
        for name, type_ in _COLUMNS:
            if name not in existing:
                batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('companies') as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
