"""add interview prep versioning and voice delivery metrics

Revision ID: ac3ef29a6dbd
Revises: 69dff8ce3927
Create Date: 2026-08-26 05:58:01.239831

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ac3ef29a6dbd'
down_revision: Union[str, Sequence[str], None] = '69dff8ce3927'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('interview_prep', sa.Column('is_active', sa.Boolean(), nullable=True))
    op.drop_constraint(op.f('interview_prep_application_id_key'), 'interview_prep', type_='unique')
    # Existing rows predate versioning -- each one is the only/current
    # prep for its application, so it's the active one.
    op.execute("UPDATE interview_prep SET is_active = true WHERE is_active IS NULL")
    op.add_column('mock_interview_turns', sa.Column('recording_duration_seconds', sa.Float(), nullable=True))
    op.add_column('mock_interview_turns', sa.Column('pause_count', sa.Integer(), nullable=True))
    op.add_column('mock_interview_turns', sa.Column('longest_pause_seconds', sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mock_interview_turns', 'longest_pause_seconds')
    op.drop_column('mock_interview_turns', 'pause_count')
    op.drop_column('mock_interview_turns', 'recording_duration_seconds')
    op.create_unique_constraint(op.f('interview_prep_application_id_key'), 'interview_prep', ['application_id'])
    op.drop_column('interview_prep', 'is_active')
