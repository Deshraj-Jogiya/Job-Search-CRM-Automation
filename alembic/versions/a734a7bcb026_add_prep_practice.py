"""add prep_practice (review schedule for the general interview prep)

Revision ID: a734a7bcb026
Revises: e9c1d4f7a2b8
Create Date: 2026-11-19 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a734a7bcb026'
down_revision: Union[str, Sequence[str], None] = 'e9c1d4f7a2b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_TABLES = ['prep_practice']


def upgrade() -> None:
    bind = op.get_bind()
    # create_all() at startup may already have made it, so create only when missing (safe to rerun on SQLite too).
    if 'prep_practice' not in set(sa.inspect(bind).get_table_names()):
        op.create_table(
            'prep_practice',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('variant_id', sa.Integer(), sa.ForeignKey('profile_variants.id'), nullable=False),
            sa.Column('card_key', sa.String(), nullable=False),
            sa.Column('box', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_rating', sa.Integer(), nullable=True),
            sa.Column('reviews', sa.Integer(), nullable=False, server_default='0'),
            sa.Column('last_reviewed_at', sa.DateTime(), nullable=True),
            sa.Column('next_due_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('variant_id', 'card_key', name='uq_prep_practice_card'),
        )
        op.create_index('ix_prep_practice_id', 'prep_practice', ['id'])
        op.create_index('ix_prep_practice_variant_id', 'prep_practice', ['variant_id'])
        op.create_index('ix_prep_practice_next_due_at', 'prep_practice', ['next_due_at'])
    if bind.dialect.name == 'postgresql':
        for table in _APP_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('prep_practice')
