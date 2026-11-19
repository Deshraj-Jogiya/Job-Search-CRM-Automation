"""add email_messages and the email sync settings (reading job emails into the CRM)

Revision ID: b5e3d8a1c7f4
Revises: a734a7bcb026
Create Date: 2026-11-19 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5e3d8a1c7f4'
down_revision: Union[str, Sequence[str], None] = 'a734a7bcb026'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_TABLES = ['email_messages']

_SETTINGS_COLUMNS = [
    sa.Column('email_sync_enabled', sa.Boolean(), nullable=True),
    sa.Column('email_sync_lookback_days', sa.Integer(), nullable=True),
    sa.Column('email_sync_last_uid', sa.Integer(), nullable=True),
    sa.Column('email_sync_uidvalidity', sa.Integer(), nullable=True),
    sa.Column('email_sync_last_run_at', sa.DateTime(), nullable=True),
    sa.Column('email_sync_last_error', sa.String(), nullable=True),
]


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    # create_all() at startup may already have made the table, so create only when missing (safe to rerun on SQLite).
    if 'email_messages' not in set(inspector.get_table_names()):
        op.create_table(
            'email_messages',
            sa.Column('id', sa.Integer(), primary_key=True),
            sa.Column('message_id', sa.String(), nullable=False),
            sa.Column('thread_id', sa.String(), nullable=True),
            sa.Column('received_at', sa.DateTime(), nullable=False),
            sa.Column('sender_name', sa.String(), nullable=True),
            sa.Column('sender_email', sa.String(), nullable=False),
            sa.Column('subject', sa.String(), nullable=True),
            sa.Column('excerpt', sa.Text(), nullable=True),
            sa.Column('kind', sa.String(), nullable=False),
            sa.Column('confidence', sa.String(), nullable=False),
            sa.Column('reason', sa.String(), nullable=True),
            sa.Column('company_guess', sa.String(), nullable=True),
            sa.Column('role_guess', sa.String(), nullable=True),
            sa.Column('job_ref', sa.String(), nullable=True),
            sa.Column('due_text', sa.String(), nullable=True),
            sa.Column('match_state', sa.String(), nullable=False, server_default='unmatched'),
            sa.Column('match_how', sa.String(), nullable=True),
            sa.Column('application_id', sa.Integer(), nullable=True),
            sa.Column('candidate_ids', sa.JSON(), nullable=True),
            sa.Column('state', sa.String(), nullable=False, server_default='pending'),
            sa.Column('action_taken', sa.String(), nullable=True),
            sa.Column('resolved_at', sa.DateTime(), nullable=True),
            sa.Column('created_at', sa.DateTime(), nullable=True),
            sa.UniqueConstraint('message_id', name='uq_email_messages_message_id'),
        )
        op.create_index('ix_email_messages_id', 'email_messages', ['id'])
        op.create_index('ix_email_messages_received_at', 'email_messages', ['received_at'])
        op.create_index('ix_email_messages_application_id', 'email_messages', ['application_id'])
        op.create_index('ix_email_messages_state', 'email_messages', ['state'])

    existing = {c['name'] for c in inspector.get_columns('global_settings')}
    for column in _SETTINGS_COLUMNS:
        if column.name not in existing:
            op.add_column('global_settings', column)
    # A column added by a migration leaves the existing settings row NULL, not the model's default.
    op.execute("UPDATE global_settings SET email_sync_enabled = false WHERE email_sync_enabled IS NULL")
    op.execute("UPDATE global_settings SET email_sync_lookback_days = 60 WHERE email_sync_lookback_days IS NULL")

    if bind.dialect.name == 'postgresql':
        for table in _APP_TABLES:
            op.execute(f'ALTER TABLE {table} ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    op.drop_table('email_messages')
    with op.batch_alter_table('global_settings') as batch:
        for column in reversed(_SETTINGS_COLUMNS):
            batch.drop_column(column.name)
