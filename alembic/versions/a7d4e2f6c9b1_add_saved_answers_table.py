"""add saved_answers table

Revision ID: a7d4e2f6c9b1
Revises: f1a3c9d0e8b2
Create Date: 2026-09-28 00:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d4e2f6c9b1'
down_revision: Union[str, Sequence[str], None] = 'f1a3c9d0e8b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Real gap found 2026-09-15 (see 7a1c2e9f4b3d): a new table that forgets
# to enable RLS runs exposed on the anon/authenticated PostgREST path on
# Supabase for as long as nobody notices. Enabled directly in the same
# migration that creates the table this time, not as a later catch-up
# pass -- and test_rls_coverage.py's own static check fails the suite if
# this table is ever left off every RLS migration's table list.
_APP_TABLES = ["saved_answers"]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'saved_answers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('question_text', sa.Text(), nullable=False),
        sa.Column('question_normalized', sa.Text(), nullable=False),
        sa.Column('answer_text', sa.Text(), nullable=False),
        sa.Column('times_reused', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('last_used_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_saved_answers_id'), 'saved_answers', ['id'], unique=False)
    op.create_index(op.f('ix_saved_answers_question_normalized'), 'saved_answers', ['question_normalized'], unique=False)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _APP_TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_saved_answers_question_normalized'), table_name='saved_answers')
    op.drop_index(op.f('ix_saved_answers_id'), table_name='saved_answers')
    op.drop_table('saved_answers')
