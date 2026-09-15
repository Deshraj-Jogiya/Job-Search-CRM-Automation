"""add research_agent_queries table

Enables RLS on the new table in the same migration that creates it --
learned directly from 7a1c2e9f4b3d's own finding (a table created without
RLS silently stayed exposed for weeks). _MISSED_TABLES named to match
test_rls_coverage.py's fallback lookup, not because this table was
"missed" in the historical sense -- it's covered from its first migration.

Revision ID: 70dba536fd0f
Revises: 7a1c2e9f4b3d
Create Date: 2026-09-15 02:14:16.981286

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '70dba536fd0f'
down_revision: Union[str, Sequence[str], None] = '7a1c2e9f4b3d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MISSED_TABLES = ["research_agent_queries"]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'research_agent_queries',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('question', sa.Text(), nullable=False),
        sa.Column('answer', sa.Text(), nullable=True),
        sa.Column('steps_json', sa.Text(), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_research_agent_queries_id'), 'research_agent_queries', ['id'], unique=False)

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _MISSED_TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_research_agent_queries_id'), table_name='research_agent_queries')
    op.drop_table('research_agent_queries')
