"""add activity_log_retention_days to global_settings

Revision ID: 63586b495108
Revises: 70dba536fd0f
Create Date: 2026-09-15 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '63586b495108'
down_revision: Union[str, Sequence[str], None] = '70dba536fd0f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('global_settings', sa.Column('activity_log_retention_days', sa.Integer(), nullable=True, server_default='90'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'activity_log_retention_days')
