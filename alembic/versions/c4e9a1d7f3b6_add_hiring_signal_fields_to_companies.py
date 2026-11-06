"""add hiring signal fields to companies

Revision ID: c4e9a1d7f3b6
Revises: b7d2f4a6c8e1
Create Date: 2026-11-06 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e9a1d7f3b6'
down_revision: Union[str, Sequence[str], None] = 'b7d2f4a6c8e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = (
    ('hiring_alert_kind', sa.String()),
    ('hiring_alert_scope', sa.String()),
    ('hiring_alert_percent', sa.Float()),
    ('hiring_alert', sa.String()),
    ('hiring_alert_until', sa.Date()),
    ('hiring_alert_source', sa.String()),
    ('board_open_total', sa.Integer()),
    ('board_data_open', sa.Integer()),
    ('board_data_new_30d', sa.Integer()),
    ('board_data_new_90d', sa.Integer()),
    ('board_checked_at', sa.DateTime()),
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
