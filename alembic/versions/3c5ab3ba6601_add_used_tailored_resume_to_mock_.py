"""add used_tailored_resume to mock_interview_sessions

Revision ID: 3c5ab3ba6601
Revises: 39d654cb523d
Create Date: 2026-09-15 02:07:11.823557

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3c5ab3ba6601'
down_revision: Union[str, Sequence[str], None] = '39d654cb523d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('mock_interview_sessions', sa.Column('used_tailored_resume', sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mock_interview_sessions', 'used_tailored_resume')
