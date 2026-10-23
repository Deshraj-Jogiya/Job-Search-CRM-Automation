"""add sponsorship alert fields to companies

Revision ID: e2b7c4a9d1f3
Revises: d5f1a8c3e7b2
Create Date: 2026-10-23 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2b7c4a9d1f3'
down_revision: Union[str, Sequence[str], None] = 'd5f1a8c3e7b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('sponsorship_alert_kind', sa.String()),
    ('sponsorship_alert', sa.String()),
    ('sponsorship_alert_until', sa.Date()),
    ('sponsorship_alert_source', sa.String()),
)


def upgrade() -> None:
    existing = {c['name'] for c in sa.inspect(op.get_bind()).get_columns('companies')}
    # create_all() at startup may already have added them (same situation as d5f1a8c3e7b2), so add only what is missing.
    with op.batch_alter_table('companies') as batch:
        for name, type_ in _COLUMNS:
            if name not in existing:
                batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('companies') as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
