"""Real gap found 2026-09-15 via a full-codebase audit: the original RLS
migration (1e948a01be48) hardcoded a table list that immediately started
drifting from app/models.py as new tables were added -- 7 tables ran with
RLS OFF on the live Supabase deployment for weeks before anyone noticed
(closed by migration 7a1c2e9f4b3d). This test makes that drift impossible
to miss again: every table in Base.metadata must appear in the union of
every RLS-enabling migration's own table list, or this fails. Doesn't
need a live Postgres connection -- it's a static check against the
migrations' own source, importable as plain Python modules despite their
non-identifier filenames."""

import importlib.util
import os

from app.models import Base

_ALEMBIC_VERSIONS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "alembic", "versions"
)

# Every migration that enables RLS on a batch of tables -- add the new
# migration's filename here whenever a future one adds more tables,
# rather than growing a single hardcoded list that drifts again.
_RLS_MIGRATION_FILES = [
    "1e948a01be48_enable_row_level_security_on_all_app_.py",
    "7a1c2e9f4b3d_enable_rls_on_tables_added_since_the_.py",
]


def _load_table_list(filename: str, attr_name: str) -> list[str]:
    path = os.path.join(_ALEMBIC_VERSIONS_DIR, filename)
    spec = importlib.util.spec_from_file_location(filename[:-3], path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, attr_name)


def test_every_app_table_is_covered_by_some_rls_migration():
    covered = set()
    for filename in _RLS_MIGRATION_FILES:
        attr_name = "_APP_TABLES" if "_APP_TABLES" in open(
            os.path.join(_ALEMBIC_VERSIONS_DIR, filename)
        ).read() else "_MISSED_TABLES"
        covered |= set(_load_table_list(filename, attr_name))

    all_app_tables = set(Base.metadata.tables.keys())
    missing = all_app_tables - covered

    assert not missing, (
        f"{sorted(missing)} exist in app/models.py but were never added to any RLS-enabling "
        "migration's table list -- add a new migration enabling RLS on them (see "
        "7a1c2e9f4b3d for the pattern), then add its filename to _RLS_MIGRATION_FILES above."
    )
