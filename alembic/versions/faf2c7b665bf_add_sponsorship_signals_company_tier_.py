"""add sponsorship signals, company tier, score breakdown, outreach hygiene, queue triage fields

Revision ID: faf2c7b665bf
Revises: 48244f109545
Create Date: 2026-09-10 20:56:38.894425

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'faf2c7b665bf'
down_revision: Union[str, Sequence[str], None] = '48244f109545'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # companies and job_applications each get a new CHECK constraint
    # below -- SQLite has no ALTER-based way to add a constraint to an
    # existing table (confirmed live: plain op.create_check_constraint
    # raises NotImplementedError against SQLite), only batch mode's
    # copy-and-move rebuild supports it there. batch_alter_table is a
    # no-op passthrough to plain ALTER on Postgres, so this is the one
    # dialect-safe way to write this that actually runs on both --
    # hand-edited from the raw autogenerate output for this reason.
    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.add_column(sa.Column('max_wage_level_15xx', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('tier', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('tier_computed_at', sa.DateTime(), nullable=True))
        batch_op.create_check_constraint('ck_companies_max_wage_level', "max_wage_level_15xx IN ('I', 'II', 'III', 'IV')")
        batch_op.create_check_constraint('ck_companies_tier', "tier IN ('A', 'B', 'C', 'X')")

    # server_default backfills the one already-existing global_settings row
    # with the real Python-side default -- same reasoning as
    # fae07861e8f5's min_score_for_auto_launch: company_tier.py/
    # outreach_hygiene.py/queue_service.py all do direct arithmetic/
    # comparison against these columns, which would raise TypeError
    # against a NULL value on the live row otherwise.
    op.add_column('global_settings', sa.Column('tier_a_min_filings', sa.Integer(), nullable=False, server_default='10'))
    op.add_column('global_settings', sa.Column('tier_a_min_wage_level', sa.Integer(), nullable=False, server_default='3'))
    op.add_column('global_settings', sa.Column('tier_b_min_filings', sa.Integer(), nullable=False, server_default='3'))
    op.add_column('global_settings', sa.Column('outreach_person_lifetime_cap', sa.Integer(), nullable=False, server_default='1'))
    op.add_column('global_settings', sa.Column('outreach_company_cap_count', sa.Integer(), nullable=False, server_default='3'))
    op.add_column('global_settings', sa.Column('outreach_company_cap_days', sa.Integer(), nullable=False, server_default='14'))
    op.add_column('global_settings', sa.Column('daily_application_target_min', sa.Integer(), nullable=False, server_default='8'))
    op.add_column('global_settings', sa.Column('daily_application_target_max', sa.Integer(), nullable=False, server_default='12'))
    op.add_column('global_settings', sa.Column('daily_outreach_touch_target', sa.Integer(), nullable=False, server_default='15'))

    with op.batch_alter_table('job_applications', schema=None) as batch_op:
        batch_op.add_column(sa.Column('replied_at', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('score_breakdown', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('skip_reason', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('skipped_at', sa.DateTime(), nullable=True))
        batch_op.create_check_constraint(
            'ck_job_applications_skip_reason',
            "skip_reason IN ('not_interested', 'low_priority', 'duplicate_role', 'bad_timing', 'other')",
        )

    op.add_column('job_postings', sa.Column('sponsorship_blocked', sa.Boolean(), nullable=True))
    op.add_column('job_postings', sa.Column('sponsorship_signal', sa.Boolean(), nullable=True))
    op.add_column('job_postings', sa.Column('worksite_ambiguous', sa.Boolean(), nullable=True))
    op.add_column('job_postings', sa.Column('signal_matches', sa.JSON(), nullable=True))
    op.add_column('outreach_messages', sa.Column('cap_override_reason', sa.String(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('outreach_messages', 'cap_override_reason')
    op.drop_column('job_postings', 'signal_matches')
    op.drop_column('job_postings', 'worksite_ambiguous')
    op.drop_column('job_postings', 'sponsorship_signal')
    op.drop_column('job_postings', 'sponsorship_blocked')

    with op.batch_alter_table('job_applications', schema=None) as batch_op:
        batch_op.drop_constraint('ck_job_applications_skip_reason', type_='check')
        batch_op.drop_column('skipped_at')
        batch_op.drop_column('skip_reason')
        batch_op.drop_column('score_breakdown')
        batch_op.drop_column('replied_at')

    op.drop_column('global_settings', 'daily_outreach_touch_target')
    op.drop_column('global_settings', 'daily_application_target_max')
    op.drop_column('global_settings', 'daily_application_target_min')
    op.drop_column('global_settings', 'outreach_company_cap_days')
    op.drop_column('global_settings', 'outreach_company_cap_count')
    op.drop_column('global_settings', 'outreach_person_lifetime_cap')
    op.drop_column('global_settings', 'tier_b_min_filings')
    op.drop_column('global_settings', 'tier_a_min_wage_level')
    op.drop_column('global_settings', 'tier_a_min_filings')

    with op.batch_alter_table('companies', schema=None) as batch_op:
        batch_op.drop_constraint('ck_companies_tier', type_='check')
        batch_op.drop_constraint('ck_companies_max_wage_level', type_='check')
        batch_op.drop_column('tier_computed_at')
        batch_op.drop_column('tier')
        batch_op.drop_column('max_wage_level_15xx')
