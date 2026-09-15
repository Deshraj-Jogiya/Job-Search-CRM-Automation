"""Real gap found 2026-09-15 via a full-codebase audit: activity_logs is
a write-only audit trail (6,216 real rows on the live instance at the
time this was found) with no retention policy anywhere -- unbounded
forever on a 500MB free Postgres tier. sweep_activity_log_retention
closes that, same GlobalSettings-driven pattern as
confirmation_service.sweep_rejected_retention."""

from datetime import timedelta

from app.database import utcnow
from app.models import ActivityLog, get_or_create_settings
from app.services.activity_logger import sweep_activity_log_retention


def _make_log(db, days_old: int, message: str = "test entry"):
    entry = ActivityLog(message=message, level="INFO", timestamp=utcnow() - timedelta(days=days_old))
    db.add(entry)
    db.commit()
    return entry


def test_deletes_entries_older_than_the_configured_retention_window(db):
    settings = get_or_create_settings(db)
    settings.activity_log_retention_days = 30
    db.commit()

    _make_log(db, days_old=45, message="old, should be swept")
    _make_log(db, days_old=10, message="recent, should stay")

    count = sweep_activity_log_retention(db)

    assert count == 1
    remaining = db.query(ActivityLog).all()
    assert len(remaining) == 1
    assert remaining[0].message == "recent, should stay"


def test_no_op_when_nothing_is_old_enough(db):
    settings = get_or_create_settings(db)
    settings.activity_log_retention_days = 90
    db.commit()

    _make_log(db, days_old=5)

    count = sweep_activity_log_retention(db)

    assert count == 0
    assert db.query(ActivityLog).count() == 1


def test_uses_the_default_retention_window_when_never_configured(db):
    # GlobalSettings.activity_log_retention_days defaults to 90 -- a real
    # entry 91 days old should sweep with no explicit setting change.
    _make_log(db, days_old=91, message="past the default window")
    _make_log(db, days_old=89, message="inside the default window")

    count = sweep_activity_log_retention(db)

    assert count == 1
    remaining = db.query(ActivityLog).all()
    assert remaining[0].message == "inside the default window"
