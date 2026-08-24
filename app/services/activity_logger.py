import logging

from ..models import ActivityLog
from ..logging_config import LOGGER_NAME

logger = logging.getLogger(LOGGER_NAME)


def log_activity(db, message: str, level: str = "INFO"):
    """Record a human-readable activity entry (visible on the dashboard)
    and mirror it to the retained log file first -- so an ERROR-level
    call made from inside an except block gets its full traceback saved
    even if the DB write below then fails (e.g. the DB is the actual
    problem), which is exactly the failure mode a plain DB-only log
    can't capture."""
    logger.log(
        getattr(logging, level.upper(), logging.INFO),
        message,
        exc_info=(level.upper() == "ERROR"),
    )
    entry = ActivityLog(message=message, level=level)
    db.add(entry)
    db.commit()


def log_exception(message: str):
    """Record a full traceback to the retained log file only, for
    failures that already have their own user-visible surface (e.g. a
    JobApplication.attention_reason) and don't need a duplicate
    dashboard activity-log row. Must be called from inside an except
    block."""
    logger.exception(message)
