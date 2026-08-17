"""
Background scheduler. Runs one tick that: polls whichever intake
sources are due (Phase 2), auto-proceeds any Pending Confirmation
application whose deadline has passed, and sweeps Rejected applications
past their retention window (Phase 4). Each of those internally checks
GlobalSettings.automation_enabled fresh before doing real work, per
CLAUDE.md's kill-switch convention -- this file just needs to fire
often enough that the shortest configured interval/deadline isn't
missed by much, it does not encode cadence itself.
"""

from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from ..models import get_or_create_settings
from . import confirmation_service, intake_service

scheduler = BackgroundScheduler()

_TICK_MINUTES = 5


def _tick() -> None:
    db = SessionLocal()
    try:
        intake_service.run_intake_cycle(db)

        settings = get_or_create_settings(db)
        if settings.automation_enabled:
            confirmation_service.sweep_expired_confirmations(db)
            confirmation_service.sweep_rejected_retention(db)
    except Exception as e:
        print(f"Error in scheduler tick: {e}")
    finally:
        db.close()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.add_job(_tick, trigger="interval", minutes=_TICK_MINUTES, name="job_intake_tick")
        scheduler.start()
        print(f"Background scheduler started (tick every {_TICK_MINUTES}m).")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
        print("Background scheduler shut down.")
