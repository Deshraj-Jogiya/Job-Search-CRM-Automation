"""add bulk discovery settings

Revision ID: c7040c0a371e
Revises: ac3ef29a6dbd
Create Date: 2026-08-27 21:01:51.837482

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7040c0a371e'
down_revision: Union[str, Sequence[str], None] = 'ac3ef29a6dbd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('global_settings', sa.Column('bulk_discovery_poll_interval_hours', sa.Integer(), nullable=True))
    op.add_column('global_settings', sa.Column('bulk_discovery_batch_size', sa.Integer(), nullable=True))
    # The model's Python-side `default=` only applies when SQLAlchemy
    # inserts a NEW row -- an already-existing global_settings row (the
    # normal case, since this table is created once via create_all() at
    # first startup and never re-created) gets these new columns as NULL
    # otherwise, which crashes _discover_companies_from_ats_dataset's
    # `>=` comparison the first time it runs. Backfill explicitly.
    op.execute("UPDATE global_settings SET bulk_discovery_poll_interval_hours = 24 WHERE bulk_discovery_poll_interval_hours IS NULL")
    op.execute("UPDATE global_settings SET bulk_discovery_batch_size = 25 WHERE bulk_discovery_batch_size IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'bulk_discovery_batch_size')
    op.drop_column('global_settings', 'bulk_discovery_poll_interval_hours')
