"""add mock interview sessions and turns

Revision ID: 884b7f2664c5
Revises: 7693693a7e2e
Create Date: 2026-08-26 05:02:35.645526

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '884b7f2664c5'
down_revision: Union[str, Sequence[str], None] = '7693693a7e2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('mock_interview_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('application_id', sa.Integer(), nullable=False),
    sa.Column('round_name', sa.String(), nullable=False),
    sa.Column('tier', sa.String(), nullable=False),
    sa.Column('status', sa.String(), nullable=False),
    sa.Column('debrief_json', sa.Text(), nullable=True),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('ended_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['application_id'], ['job_applications.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mock_interview_sessions_id'), 'mock_interview_sessions', ['id'], unique=False)
    op.create_table('mock_interview_turns',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('turn_index', sa.Integer(), nullable=False),
    sa.Column('speaker', sa.String(), nullable=False),
    sa.Column('content', sa.Text(), nullable=False),
    sa.Column('is_followup', sa.Boolean(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['mock_interview_sessions.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_mock_interview_turns_id'), 'mock_interview_turns', ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_mock_interview_turns_id'), table_name='mock_interview_turns')
    op.drop_table('mock_interview_turns')
    op.drop_index(op.f('ix_mock_interview_sessions_id'), table_name='mock_interview_sessions')
    op.drop_table('mock_interview_sessions')
