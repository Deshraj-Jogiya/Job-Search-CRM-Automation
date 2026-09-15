"""add llm_usage_logs table

Revision ID: 4938ed06d95b
Revises: b8ec1b14d258
Create Date: 2026-09-14 04:34:48.034463

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4938ed06d95b'
down_revision: Union[str, Sequence[str], None] = 'b8ec1b14d258'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema. Autogenerate also proposed a batch of unrelated
    global_settings nullable-constraint changes (pre-existing drift
    between the live DB and the model defaults) -- stripped out here,
    out of scope for this migration and not verified safe to apply."""
    op.create_table('llm_usage_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider', sa.String(), nullable=False),
    sa.Column('model', sa.String(), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('estimated_cost_usd', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_llm_usage_logs_id'), 'llm_usage_logs', ['id'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_llm_usage_logs_id'), table_name='llm_usage_logs')
    op.drop_table('llm_usage_logs')
