"""add camera opt-in and visual metrics to mock interview sessions

Revision ID: 69dff8ce3927
Revises: 67c2ac36748d
Create Date: 2026-08-26 05:38:03.134605

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '69dff8ce3927'
down_revision: Union[str, Sequence[str], None] = '67c2ac36748d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('mock_interview_sessions', sa.Column('camera_enabled', sa.Boolean(), nullable=True))
    op.add_column('mock_interview_sessions', sa.Column('visual_metrics_json', sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('mock_interview_sessions', 'visual_metrics_json')
    op.drop_column('mock_interview_sessions', 'camera_enabled')
