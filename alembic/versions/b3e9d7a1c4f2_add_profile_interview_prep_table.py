"""add profile_interview_prep table

Revision ID: b3e9d7a1c4f2
Revises: a7d4e2f6c9b1
Create Date: 2026-10-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3e9d7a1c4f2'
down_revision: Union[str, Sequence[str], None] = 'a7d4e2f6c9b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# RLS enabled in the same migration that creates the table (see 7a1c2e9f4b3d
# for why), and test_rls_coverage.py fails the suite if it is ever omitted.
_APP_TABLES = ["profile_interview_prep"]


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'profile_interview_prep',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('variant_id', sa.Integer(), nullable=False),
        sa.Column('content_json', sa.Text(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=True),
        sa.Column('generated_at', sa.DateTime(), nullable=True),
        sa.Column('profile_version_id', sa.Integer(), nullable=True),
        sa.Column('trigger', sa.String(), nullable=True),
        sa.ForeignKeyConstraint(['variant_id'], ['profile_variants.id']),
        sa.ForeignKeyConstraint(['profile_version_id'], ['profile_versions.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_profile_interview_prep_id'), 'profile_interview_prep', ['id'], unique=False)
    op.add_column(
        'global_settings',
        sa.Column('profile_prep_auto_refresh', sa.Boolean(), nullable=False, server_default=sa.text('true')),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for table in _APP_TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('global_settings', 'profile_prep_auto_refresh')
    op.drop_index(op.f('ix_profile_interview_prep_id'), table_name='profile_interview_prep')
    op.drop_table('profile_interview_prep')
