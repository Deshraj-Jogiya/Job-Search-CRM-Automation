"""add used_tailored_resume to interview_prep

Revision ID: 39d654cb523d
Revises: 4938ed06d95b
Create Date: 2026-09-15 01:56:08.739961

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '39d654cb523d'
down_revision: Union[str, Sequence[str], None] = '4938ed06d95b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('interview_prep', sa.Column('used_tailored_resume', sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('interview_prep', 'used_tailored_resume')
