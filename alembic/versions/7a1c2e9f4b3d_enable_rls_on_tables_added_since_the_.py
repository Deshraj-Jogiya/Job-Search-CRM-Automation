"""enable RLS on tables added since the original RLS pass

Real gap found 2026-09-15 via a full-codebase audit: the original RLS
migration (1e948a01be48, 2026-08-24) hardcoded a list of 15 tables that
existed at the time. Seven tables created by later migrations were never
added to that list and have been running with RLS OFF on the live
Supabase deployment ever since -- exposed to the anon/authenticated
PostgREST path exactly as 1e948a01be48's own docstring describes,
including real personal data (mock_interview_sessions/turns transcripts,
behavioral_stories) and cost data (llm_usage_logs).

Same posture as the original migration: zero policies, so this changes
nothing for the app itself (it connects with real DB credentials that
bypass RLS by Supabase's own design) -- it only closes the anon-key
PostgREST path this app never used. SQLite no-op, same dialect guard.

Revision ID: 7a1c2e9f4b3d
Revises: 3c5ab3ba6601
Create Date: 2026-09-15 02:15:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '7a1c2e9f4b3d'
down_revision: Union[str, Sequence[str], None] = '3c5ab3ba6601'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_MISSED_TABLES = [
    "oews_wages",
    "llm_usage_logs",
    "behavioral_stories",
    "mock_interview_sessions",
    "mock_interview_turns",
    "adaptation_log",
    "adaptive_parameter_values",
]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _MISSED_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _MISSED_TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
