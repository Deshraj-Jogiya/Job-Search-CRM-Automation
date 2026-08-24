"""add automated backup settings to global_settings

Revision ID: 5b48b4e511e0
Revises: 6328da1202c8
Create Date: 2026-08-24 05:08:08.521364

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5b48b4e511e0'
down_revision: Union[str, Sequence[str], None] = '6328da1202c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Explicit server_default (rather than autogenerate's bare nullable=True)
    # so the existing global_settings row -- there's exactly one, this table
    # is a singleton -- gets a real value instead of NULL, which would
    # otherwise silently read as "backups disabled" despite the model's own
    # default=True.
    op.add_column(
        'global_settings',
        sa.Column('automated_backups_enabled', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )
    op.add_column(
        'global_settings',
        sa.Column('backup_retention_count', sa.Integer(), nullable=False, server_default=sa.text('14')),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'backup_retention_count')
    op.drop_column('global_settings', 'automated_backups_enabled')
