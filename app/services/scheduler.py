"""
Background scheduler. Runs one tick that: polls whichever job sources
are due, auto-proceeds any Pending Confirmation application whose
deadline has passed, sweeps Rejected applications past their retention
window, and sends the notification digest if anything new is queued
and the digest interval has elapsed. Each of those internally checks
GlobalSettings.automation_enabled fresh before doing real work, so a
mid-run toggle takes effect immediately -- this file just needs to
fire often enough that the shortest configured interval/deadline isn't
missed by much, it does not encode cadence itself.

Each of the 4 concerns above runs in its own DB session with its own
exception isolation. An earlier version shared one session
and one try/except across all of them -- an unhandled error in intake
(e.g. a source's network call throwing past its own internal handling)
silently skipped the confirmation sweeps and the digest for that whole
tick too, even though they're logically independent. Failures are
logged via log_activity (visible in the dashboard) rather than just
printed to a console nobody's watching, since this is meant to run
unattended.
"""

from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from ..models import get_or_create_settings
from . import backup_service, confirmation_service, intake_service, notification_service, trend_research_service
from .activity_logger import log_activity, sweep_activity_log_retention

scheduler = BackgroundScheduler()

_TICK_MINUTES = 5
_BACKUP_INTERVAL_HOURS = 24
# Real-world practice around resume length/format moves slowly (this is
# what motivated building trend_research_service.py in the first place --
# a stale, never-re-verified assumption sat in config for a long time) --
# monthly is frequent enough to catch a real shift without spending
# Tavily/LLM budget checking something that hasn't meaningfully changed
# since last week.
_TREND_CHECK_INTERVAL_DAYS = 30


def _run_isolated(name: str, fn) -> None:
    db = SessionLocal()
    try:
        fn(db)
    except Exception as e:
        try:
            log_activity(db, f"Scheduler tick: {name} failed -- {e}", "ERROR")
        except Exception:
            print(f"Error in scheduler tick ({name}): {e}")
    finally:
        db.close()


def _run_if_automation_enabled(name: str, fn) -> None:
    def _guarded(db):
        settings = get_or_create_settings(db)
        if settings.automation_enabled:
            fn(db)

    _run_isolated(name, _guarded)


def _tick() -> None:
    _run_isolated("intake", intake_service.run_intake_cycle)
    _run_if_automation_enabled("expired-confirmation sweep", confirmation_service.sweep_expired_confirmations)
    _run_if_automation_enabled("rejected-retention sweep", confirmation_service.sweep_rejected_retention)
    _run_if_automation_enabled("notification digest", notification_service.send_digest)


def _backup_tick() -> None:
    # Deliberately not gated by automation_enabled -- that's a job-hunting
    # kill switch, not a "stop protecting my data" switch. Its own
    # automated_backups_enabled setting is the only gate (checked inside
    # run_scheduled_backup).
    _run_isolated("scheduled backup", backup_service.run_scheduled_backup)


def _activity_log_retention_tick() -> None:
    # Deliberately not gated by automation_enabled either, same reasoning
    # as backups -- this is data hygiene (bounding an unbounded audit
    # table), not the job search itself, so it should keep working even
    # while automation is paused.
    _run_isolated("activity-log retention sweep", sweep_activity_log_retention)


def _trend_check_tick() -> None:
    # Deliberately not gated by automation_enabled either, same reasoning
    # as backups -- this keeps the RESUME-BUILDING RULES current, not the
    # job search itself, so it should keep working even while automation
    # is paused. run_all_trend_checks already no-ops safely if Tavily
    # isn't configured, or if a proposal for a category is already
    # pending review -- never spends budget re-checking something a
    # human hasn't looked at yet.
    _run_isolated("trend check", lambda db: trend_research_service.run_all_trend_checks(db, date.today().year))


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.add_job(_tick, trigger="interval", minutes=_TICK_MINUTES, name="job_intake_tick")
        scheduler.add_job(_backup_tick, trigger="interval", hours=_BACKUP_INTERVAL_HOURS, name="scheduled_backup")
        scheduler.add_job(
            _activity_log_retention_tick, trigger="interval", hours=_BACKUP_INTERVAL_HOURS, name="activity_log_retention",
        )
        scheduler.add_job(_trend_check_tick, trigger="interval", days=_TREND_CHECK_INTERVAL_DAYS, name="trend_check")
        scheduler.start()
        print(
            f"Background scheduler started (tick every {_TICK_MINUTES}m, backup every "
            f"{_BACKUP_INTERVAL_HOURS}h, trend check every {_TREND_CHECK_INTERVAL_DAYS}d)."
        )


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
        print("Background scheduler shut down.")
