"""scheduler.py's own tick functions. Real gap found live 2026-09-18:
score/tailor used to run inside the same 5-minute _tick() as intake,
sitting behind intake's own slower work before it ever started, while
intake regularly outpaces it (222 new postings in a single cycle).
Split onto its own, independent, shorter-interval job (_progress_tick)
-- these tests lock in that the split actually happened, not just that
each piece works in isolation."""

from app.models import get_or_create_settings
from app.services import scheduler


def test_tick_no_longer_calls_progress_ingested_applications(db, monkeypatch):
    # Deliberately NOT patching scheduler.SessionLocal -- _run_isolated
    # creates and closes its own session per call (matches real
    # production behavior); forcing it to reuse this test's own `db`
    # fixture session would have that close() call tear down the
    # fixture out from under the rest of the test. A fresh SessionLocal()
    # here still points at the same file-based test SQLite DB this
    # fixture already set up, so the settings.automation_enabled write
    # below is visible to it regardless.
    calls = []
    monkeypatch.setattr(
        scheduler.confirmation_service, "progress_ingested_applications", lambda db: calls.append("progress")
    )
    monkeypatch.setattr(scheduler.intake_service, "run_intake_cycle", lambda db: calls.append("intake"))
    monkeypatch.setattr(scheduler.confirmation_service, "sweep_expired_confirmations", lambda db: None)
    monkeypatch.setattr(scheduler.confirmation_service, "sweep_rejected_retention", lambda db: None)
    monkeypatch.setattr(scheduler.notification_service, "send_digest", lambda db: None)

    settings = get_or_create_settings(db)
    settings.automation_enabled = True
    db.commit()

    scheduler._tick()

    assert "progress" not in calls
    assert "intake" in calls


def test_progress_tick_calls_progress_ingested_applications_when_automation_enabled(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler.confirmation_service, "progress_ingested_applications", lambda db: calls.append("progress")
    )

    settings = get_or_create_settings(db)
    settings.automation_enabled = True
    db.commit()

    scheduler._progress_tick()

    assert calls == ["progress"]


def test_progress_tick_does_nothing_when_automation_disabled(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        scheduler.confirmation_service, "progress_ingested_applications", lambda db: calls.append("progress")
    )

    settings = get_or_create_settings(db)
    settings.automation_enabled = False
    db.commit()

    scheduler._progress_tick()

    assert calls == []
