"""add upgrade_reports table

Revision ID: d5f1a8c3e7b2
Revises: b3e9d7a1c4f2
Create Date: 2026-10-22 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd5f1a8c3e7b2'
down_revision: Union[str, Sequence[str], None] = 'b3e9d7a1c4f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# RLS enabled in the same migration that creates the table (see 7a1c2e9f4b3d
# for why), and test_rls_coverage.py fails the suite if it is ever omitted.
_APP_TABLES = ["upgrade_reports"]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    # The app's create_all() at startup can create the table before this runs (a restart ahead of the
    # migration, as happened on the demo); then only RLS is left to do.
    if not sa.inspect(bind).has_table('upgrade_reports'):
        _create()
    if bind.dialect.name == "postgresql":
        for table in _APP_TABLES:
            op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def _create() -> None:
    op.create_table(
        'upgrade_reports',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('is_quarterly', sa.Boolean(), nullable=True),
        sa.Column('report_json', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_upgrade_reports_id'), 'upgrade_reports', ['id'], unique=False)
    op.create_index(op.f('ix_upgrade_reports_created_at'), 'upgrade_reports', ['created_at'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_upgrade_reports_created_at'), table_name='upgrade_reports')
    op.drop_index(op.f('ix_upgrade_reports_id'), table_name='upgrade_reports')
    op.drop_table('upgrade_reports')
