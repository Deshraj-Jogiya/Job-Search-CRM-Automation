"""baseline: existing schema as of 2026-08-23

Revision ID: 88907bf5c881
Revises:
Create Date: 2026-08-23 02:15:20.600759

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '88907bf5c881'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline revision -- the live DB is stamped at this revision
    without ever executing it (see ARCHITECTURE.md/CLAUDE.md), so this
    intentionally has nothing to do: it exists only to give Alembic a
    starting point that matches the schema Base.metadata.create_all()
    already built. Real column/table additions from here on get their
    own migration via `alembic revision --autogenerate`.

    autogenerate's first pass here also proposed dropping 4 tables
    (portfolio_messages, linkedin_comments, chatbot_cache, portfolio_
    visits) that exist in this Supabase project but aren't in this
    app's models.py -- confirmed by their columns (visitor_id/page_path,
    a question/answer chatbot cache, LinkedIn comment_id/author_urn,
    a contact-form name/email/message) that these belong to a separate
    application (the portfolio site) sharing this same Supabase
    project, not leftover cruft from this project's own old prototype.
    Deliberately removed from this migration -- Alembic must never
    manage or touch tables it doesn't own, no matter what autogenerate
    proposes."""
    pass


def downgrade() -> None:
    """No-op to match upgrade() -- see its docstring."""
    pass
