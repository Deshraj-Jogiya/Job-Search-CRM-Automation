"""enable row level security on all app tables

Supabase's own security linter flags every table in the public schema
that's exposed via PostgREST (its auto-generated REST API, always on
for a Supabase project) but doesn't have Row Level Security enabled --
anyone holding the project's `anon` key can otherwise read/write it
directly over HTTPS, entirely bypassing this app's own auth/CSRF. This
app itself never queries through PostgREST (it connects directly via
psycopg2/SQLAlchemy using real DB credentials, which bypass RLS by
Supabase's own design for the owning `postgres` role), so enabling RLS
here with zero policies changes nothing for the app -- it only closes
off the anon/authenticated PostgREST path this app was never using.

Real, non-hypothetical risk given this project's specific setup: this
Supabase project also hosts the portfolio site's own tables in the same
public schema, and that site's frontend plausibly embeds the anon key
client-side (a normal pattern for its own contact-form/chatbot use of
Supabase) -- an anon key that's public by design would otherwise be
able to reach this app's real job-application/profile data too.

SQLite has no RLS concept, so this migration is a genuine no-op there
(only matters for the real Postgres/Supabase deployment) -- guarded by
checking engine dialect so a SQLite fork's migration run doesn't error.

Deliberately scoped to only this app's own tables (from Base.metadata),
never the portfolio site's four foreign tables that alembic/env.py's
include_object already excludes elsewhere -- those aren't owned by
this project and altering them isn't this migration's call to make.

Revision ID: 1e948a01be48
Revises: 366cc063786a
Create Date: 2026-08-24 01:04:51.952216

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '1e948a01be48'
down_revision: Union[str, Sequence[str], None] = '366cc063786a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_APP_TABLES = [
    "admin_accounts",
    "profile_variants",
    "profile_versions",
    "companies",
    "job_postings",
    "job_applications",
    "tailored_documents",
    "outreach_messages",
    "interview_prep",
    "search_keywords",
    "seniority_exclusions",
    "location_exclusions",
    "job_sources",
    "global_settings",
    "activity_logs",
]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _APP_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    for table in _APP_TABLES:
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')
