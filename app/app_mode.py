"""
Two-face packaging. Same codebase, config-driven:

    Personal instance: real profile, real credentials, full automation.
    Public showcase: demo profile, automation off by default, clear
    setup + ethical-use docs.

APP_MODE selects which face is active. Defaults to "personal" so the
existing, actively-used real instance is completely unaffected unless
this is explicitly set to "showcase" somewhere else (e.g. a separate
fork/deployment meant for public demo purposes).
"""

import os


def is_showcase_mode() -> bool:
    return os.getenv("APP_MODE", "personal").strip().lower() == "showcase"


def assert_database_url_safe_for_mode(database_url: str) -> None:
    """Guards the one demo-instance mistake with real consequences:
    APP_MODE=showcase pointed at the real personal instance's Supabase
    project. Raises loudly at startup (see database.py, which calls
    this right after computing DATABASE_URL) rather than silently
    running the public demo against real production data. A plain
    function rather than import-time logic in database.py itself so
    it stays directly unit-testable with arbitrary (mode, url)
    combinations without needing to reimport the module."""
    if is_showcase_mode() and "supabase" in (database_url or "").lower():
        raise RuntimeError(
            "APP_MODE=showcase (demo instance) but DATABASE_URL points at a Supabase host -- "
            "refusing to start. The demo instance must use its own isolated database (e.g. a "
            "local SQLite file), never the real Supabase project."
        )
