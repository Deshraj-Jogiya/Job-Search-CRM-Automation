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

from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from ..models import get_or_create_settings
from . import confirmation_service, intake_service, notification_service
from .activity_logger import log_activity

scheduler = BackgroundScheduler()

_TICK_MINUTES = 5


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


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.add_job(_tick, trigger="interval", minutes=_TICK_MINUTES, name="job_intake_tick")
        scheduler.start()
        print(f"Background scheduler started (tick every {_TICK_MINUTES}m).")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
        print("Background scheduler shut down.")
