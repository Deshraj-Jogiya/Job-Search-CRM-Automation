import logging
from datetime import timedelta

from ..database import utcnow
from ..models import ActivityLog, get_or_create_settings
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


def sweep_activity_log_retention(db) -> int:
    """Real gap found 2026-09-15 via a full-codebase audit: this table
    is a write-only audit trail (6,216 real rows on the live instance at
    the time this was found) with no retention policy anywhere --
    unbounded forever on a 500MB free Postgres tier. Bulk DELETE, not a
    per-row ORM loop like sweep_rejected_retention -- ActivityLog has no
    relationships/cascades to worry about, and this table is exactly the
    high-volume case a bulk delete exists for. Deliberately does NOT log
    its own sweep via log_activity (unlike sweep_rejected_retention) --
    that would just be one more row immediately eligible for the next
    sweep, forever."""
    settings = get_or_create_settings(db)
    cutoff = utcnow() - timedelta(days=settings.activity_log_retention_days)
    count = db.query(ActivityLog).filter(ActivityLog.timestamp < cutoff).delete(synchronize_session=False)
    if count:
        db.commit()
    return count
