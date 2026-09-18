"""add auto_score_batch_size and min_score_for_auto_tailor settings

Revision ID: 21aac715f03e
Revises: 63586b495108
Create Date: 2026-09-18 01:25:18.872111

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '21aac715f03e'
down_revision: Union[str, Sequence[str], None] = '63586b495108'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Only the 2 real new columns -- autogenerate also picked up a batch
    # of pre-existing, unrelated nullable=True vs NOT NULL drift on other
    # global_settings columns (models.py never declared them explicitly
    # nullable=False even though the live DB has them NOT NULL from an
    # earlier migration); stripped out deliberately rather than silently
    # loosening constraints on columns this change has nothing to do with.
    op.add_column('global_settings', sa.Column('auto_score_batch_size', sa.Integer(), nullable=True))
    op.add_column('global_settings', sa.Column('min_score_for_auto_tailor', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'min_score_for_auto_tailor')
    op.drop_column('global_settings', 'auto_score_batch_size')
