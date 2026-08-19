"""
Phase 8: two-face packaging. Same codebase, config-driven, per
CLAUDE.md's "Two-face product" section:

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
