"""add level-up suggestion fields to mock interview turns

Revision ID: 67c2ac36748d
Revises: 884b7f2664c5
Create Date: 2026-08-26 05:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '67c2ac36748d'
down_revision: Union[str, Sequence[str], None] = '884b7f2664c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('mock_interview_turns', sa.Column('suggest_level_up', sa.Boolean(), nullable=True))
    op.add_column('mock_interview_turns', sa.Column('level_up_note', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mock_interview_turns', 'level_up_note')
    op.drop_column('mock_interview_turns', 'suggest_level_up')
