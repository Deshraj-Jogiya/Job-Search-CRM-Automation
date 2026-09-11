"""add remote board poll interval setting

Revision ID: c1c1c6f2ee70
Revises: faf2c7b665bf
Create Date: 2026-09-10 21:27:01.821184

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1c1c6f2ee70'
down_revision: Union[str, Sequence[str], None] = 'faf2c7b665bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default backfills the one already-existing global_settings
    # row -- same reasoning as every other GlobalSettings column added
    # this pass (see faf2c7b665bf): intake_service.py's polling cadence
    # check does direct arithmetic against this column.
    #
    # (autogenerate also proposed nullable=True alter_columns on the 9
    # columns faf2c7b665bf just added -- cosmetic noise from Alembic not
    # being able to tell a server_default-backed column was always
    # intended non-nullable; left untouched here, not this migration's
    # concern.)
    op.add_column(
        'global_settings',
        sa.Column('remote_board_poll_interval_minutes', sa.Integer(), nullable=False, server_default='360'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'remote_board_poll_interval_minutes')
