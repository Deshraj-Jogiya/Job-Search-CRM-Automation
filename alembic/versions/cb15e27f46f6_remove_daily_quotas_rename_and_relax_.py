"""remove daily quotas, rename and relax outreach hygiene caps

Revision ID: cb15e27f46f6
Revises: c1c1c6f2ee70
Create Date: 2026-09-11 00:49:32.650581

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'cb15e27f46f6'
down_revision: Union[str, Sequence[str], None] = 'c1c1c6f2ee70'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # This app sets, displays, or enforces no target/quota number of
    # applications or outreach messages per day or week -- it counts
    # what happened, it never tells the user what they should do (see
    # models.py's GlobalSettings docstring). Drops every column that
    # existed only to hold such a number.
    #
    # server_default backfills the one already-existing global_settings
    # row with the real Python-side defaults, same reasoning as every
    # other GlobalSettings column added this pass: outreach_hygiene.py
    # does direct arithmetic against these.
    #
    # batch_alter_table: SQLite older than 3.35 has no DROP COLUMN, so on SQLite the table is rebuilt; on Postgres these are plain ALTERs.
    # Columns that already exist are skipped, so a run interrupted half-way on SQLite (which cannot roll back DDL) can be re-run.
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("global_settings")}
    with op.batch_alter_table("global_settings") as batch:
        for name, default in (("outreach_per_person_lifetime", "1"), ("outreach_per_company_max", "10"),
                              ("outreach_per_company_window_days", "14")):
            if name not in existing:
                batch.add_column(sa.Column(name, sa.Integer(), nullable=False, server_default=default))
        for name in ("daily_application_target_min", "daily_application_target_max", "outreach_company_cap_count",
                     "outreach_person_lifetime_cap", "daily_outreach_touch_target", "daily_outreach_cap", "outreach_company_cap_days"):
            if name in existing:
                batch.drop_column(name)
    # (autogenerate also proposed nullable=True alter_columns on 4
    # unrelated columns from earlier migrations -- cosmetic Alembic
    # drift, not this migration's concern, left untouched.)


def downgrade() -> None:
    """Downgrade schema."""
    op.add_column('global_settings', sa.Column('outreach_company_cap_days', sa.INTEGER(), server_default=sa.text("'14'"), nullable=False))
    op.add_column('global_settings', sa.Column('daily_outreach_cap', sa.INTEGER(), nullable=True))
    op.add_column('global_settings', sa.Column('daily_outreach_touch_target', sa.INTEGER(), server_default=sa.text("'15'"), nullable=False))
    op.add_column('global_settings', sa.Column('outreach_person_lifetime_cap', sa.INTEGER(), server_default=sa.text("'1'"), nullable=False))
    op.add_column('global_settings', sa.Column('outreach_company_cap_count', sa.INTEGER(), server_default=sa.text("'3'"), nullable=False))
    op.add_column('global_settings', sa.Column('daily_application_target_max', sa.INTEGER(), server_default=sa.text("'12'"), nullable=False))
    op.add_column('global_settings', sa.Column('daily_application_target_min', sa.INTEGER(), server_default=sa.text("'8'"), nullable=False))

    op.drop_column('global_settings', 'outreach_per_company_window_days')
    op.drop_column('global_settings', 'outreach_per_company_max')
    op.drop_column('global_settings', 'outreach_per_person_lifetime')
