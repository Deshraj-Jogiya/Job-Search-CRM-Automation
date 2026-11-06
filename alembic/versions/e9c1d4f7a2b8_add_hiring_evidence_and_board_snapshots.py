"""add hiring evidence, board snapshots and measured activity fields

Revision ID: e9c1d4f7a2b8
Revises: c4e9a1d7f3b6
Create Date: 2026-11-06 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9c1d4f7a2b8'
down_revision: Union[str, Sequence[str], None] = 'c4e9a1d7f3b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_TABLES = ['hiring_evidence', 'company_board_snapshots']

_COLUMNS = (
    ('hiring_alert_evidence', sa.String()),
    ('workday_board', sa.String()),
    ('board_new_30d_all', sa.Integer()),
    ('board_new_31_90d_all', sa.Integer()),
    ('board_falling', sa.Boolean()),
    ('linkedin_data_new_30d', sa.Integer()),
    ('linkedin_checked_at', sa.DateTime()),
    ('li_openings', sa.Integer()),
    ('li_openings_3m_pct', sa.Float()),
    ('li_openings_6m_pct', sa.Float()),
    ('li_tech_openings_3m_pct', sa.Float()),
    ('li_tech_openings_6m_pct', sa.Float()),
    ('li_checked_at', sa.DateTime()),
    ('roles_pulled_120d', sa.Integer()),
    ('own_silent_applied', sa.Integer()),
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {c['name'] for c in inspector.get_columns('companies')}
    # create_all() at startup may already have added these, so add only what is missing.
    with op.batch_alter_table('companies') as batch:
        for name, type_ in _COLUMNS:
            if name not in existing:
                batch.add_column(sa.Column(name, type_, nullable=True))
    tables = set(inspector.get_table_names())
    if 'hiring_evidence' not in tables:
        op.create_table(
            'hiring_evidence',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=True, index=True),
            sa.Column('company_name', sa.String(), nullable=False),
            sa.Column('kind', sa.String(), nullable=False),
            sa.Column('event_date', sa.Date(), nullable=False),
            sa.Column('note', sa.String(), nullable=False),
            sa.Column('source', sa.String(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('company_name', 'kind', 'event_date', 'note', name='uq_hiring_evidence'),
        )
    if 'company_board_snapshots' not in tables:
        op.create_table(
            'company_board_snapshots',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('company_id', sa.Integer(), sa.ForeignKey('companies.id'), nullable=False, index=True),
            sa.Column('taken_on', sa.Date(), nullable=False),
            sa.Column('open_total', sa.Integer(), nullable=False),
            sa.Column('data_open', sa.Integer(), nullable=False),
            sa.Column('new_30d', sa.Integer(), nullable=False),
            sa.Column('new_90d', sa.Integer(), nullable=False),
            sa.UniqueConstraint('company_id', 'taken_on', name='uq_company_board_snapshot'),
        )
    if bind.dialect.name == 'postgresql':
        for table in _APP_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('company_board_snapshots')
    op.drop_table('hiring_evidence')
    with op.batch_alter_table('companies') as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
