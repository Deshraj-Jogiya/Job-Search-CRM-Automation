"""The quota-removal migration must run on SQLite builds without DROP COLUMN and survive a re-run after a half-applied attempt.

The demo instance's SQLite (3.34) has no DROP COLUMN, and SQLite cannot roll DDL back, so a first failed attempt left the three new
columns behind and every retry then failed on "duplicate column name".
"""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_quota_removal_migration_reruns_after_partial_apply(tmp_path):
    db = tmp_path / "demo.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE global_settings (id INTEGER PRIMARY KEY, daily_application_target_min INTEGER NOT NULL DEFAULT 8, "
        "daily_application_target_max INTEGER NOT NULL DEFAULT 12, outreach_company_cap_count INTEGER NOT NULL DEFAULT 3, "
        "outreach_person_lifetime_cap INTEGER NOT NULL DEFAULT 1, daily_outreach_touch_target INTEGER NOT NULL DEFAULT 15, "
        "daily_outreach_cap INTEGER, outreach_company_cap_days INTEGER NOT NULL DEFAULT 14, "
        "outreach_per_person_lifetime INTEGER NOT NULL DEFAULT 1)"  # left behind by the interrupted first attempt
    )
    con.execute("INSERT INTO global_settings (id) VALUES (1)")
    con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
    con.execute("INSERT INTO alembic_version VALUES ('c1c1c6f2ee70')")
    con.commit()
    con.close()

    env = {**os.environ, "DATABASE_URL": f"sqlite:///{db.as_posix()}"}
    run = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "cb15e27f46f6"], cwd=REPO, env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr[-2000:]

    con = sqlite3.connect(db)
    cols = {row[1] for row in con.execute("PRAGMA table_info(global_settings)")}
    version = con.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    row = con.execute("SELECT outreach_per_company_max, outreach_per_company_window_days FROM global_settings WHERE id = 1").fetchone()
    con.close()
    assert version == "cb15e27f46f6"
    assert {"outreach_per_person_lifetime", "outreach_per_company_max", "outreach_per_company_window_days"} <= cols
    assert not cols & {"daily_application_target_min", "daily_outreach_cap", "outreach_company_cap_days"}
    assert row == (10, 14)
