"""add daily_llm_spend_cap_usd to global_settings

Revision ID: f1a3c9d0e8b2
Revises: 21aac715f03e
Create Date: 2026-09-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a3c9d0e8b2'
down_revision: Union[str, Sequence[str], None] = '21aac715f03e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('global_settings', sa.Column('daily_llm_spend_cap_usd', sa.Float(), nullable=True))
    # Backfill the existing singleton settings row -- same gotcha
    # documented in confirmation_service.py: a migration-added nullable
    # column on a pre-existing row never gets the Column's Python-side
    # default, only a brand-new row does.
    op.execute("UPDATE global_settings SET daily_llm_spend_cap_usd = 5.0 WHERE daily_llm_spend_cap_usd IS NULL")


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'daily_llm_spend_cap_usd')
