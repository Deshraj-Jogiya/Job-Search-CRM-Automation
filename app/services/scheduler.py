"""
Background scheduler for job intake. Runs run_intake_cycle() on a fixed
tick; the cycle itself decides per-call whether each source is actually
due (see intake_service._is_due) and whether automation is enabled at
all (the kill switch), so this file just needs to fire often enough
that the shortest configured interval (fast_poll_interval_minutes)
isn't missed by much -- it does not encode cadence itself.
"""

from apscheduler.schedulers.background import BackgroundScheduler

from ..database import SessionLocal
from . import intake_service

scheduler = BackgroundScheduler()

_TICK_MINUTES = 5


def _tick() -> None:
    db = SessionLocal()
    try:
        intake_service.run_intake_cycle(db)
    except Exception as e:
        print(f"Error in scheduler tick: {e}")
    finally:
        db.close()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.add_job(_tick, trigger="interval", minutes=_TICK_MINUTES, name="job_intake_tick")
        scheduler.start()
        print(f"Background job intake scheduler started (tick every {_TICK_MINUTES}m).")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown()
        print("Background scheduler shut down.")
