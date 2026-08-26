"""add interview prep answer target to global settings

Revision ID: 7693693a7e2e
Revises: 5c5f381763b8
Create Date: 2026-08-26 04:25:53.378438

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7693693a7e2e'
down_revision: Union[str, Sequence[str], None] = '5c5f381763b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('global_settings', sa.Column('interview_prep_answer_target', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'interview_prep_answer_target')
