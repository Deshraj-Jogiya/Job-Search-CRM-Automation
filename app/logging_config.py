"""
Structured, retained logging -- stdlib `logging` writing to a
size-rotated local file, layered underneath the existing DB-backed
`ActivityLog` (which stays the dashboard's human-readable recent-activity
view, unchanged).

Why a local rotating file over an external log/APM service: this project
runs at $0 with no assumed network dependency for its own operability --
adding a third-party logging service would mean a new signup, a new API
key, and a new failure mode (the service itself being unreachable) for a
feature whose entire point is resilience. A local file has none of that,
works identically in local dev and on the eventual Oracle VM, and is the
one channel that survives the exact failure this was built to catch: the
DB itself being unreachable (Supabase downtime, network blip). Size-based
rotation (5MB x 5 backups = 25MB cap) keeps this bounded on the Oracle
micro instance's limited disk without any manual cleanup.

The stdlib module is deliberately kept invisible to the rest of the app:
services still call `activity_logger.log_activity()`/`log_exception()`
exactly as before. This module only wires up where those calls end up.
"""

import logging
import logging.handlers
import os

_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "app.log")

LOGGER_NAME = "career_pilot"


def configure_logging() -> None:
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return  # already configured -- avoid duplicate handlers on reload/re-import
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    os.makedirs(_LOG_DIR, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        _LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
