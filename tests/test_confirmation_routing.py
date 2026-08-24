"""Confirmation-queue routing: evaluate_and_enqueue() must send a
tailored application down exactly one of three paths, and the
quiet-hours deadline math must never let a deadline land inside the
configured unreachable window. Playwright is never invoked here --
launch_autofill_in_background is patched out so these tests can't
accidentally open a real browser."""

from datetime import datetime, timedelta
from unittest.mock import patch

from conftest import make_application, make_company, make_posting

from app.database import utcnow
from app.services import confirmation_service


def test_flagged_application_goes_to_needs_review_with_no_deadline(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    application = make_application(db, posting, attention_reason="Possible fabrication warning")

    result = confirmation_service.evaluate_and_enqueue(db, application.id)

    assert result.status == "Needs Review"
    assert result.confirmation_deadline is None


def test_clean_autofill_supported_application_auto_approves_and_launches_browser(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    application = make_application(db, posting)

    with patch(
        "app.services.autofill_service.launch_autofill_in_background"
    ) as launch_mock:
        result = confirmation_service.evaluate_and_enqueue(db, application.id)

    assert result.status == "Approved"
    assert result.confirmed_by_user is False
    assert result.confirmation_deadline is None
    launch_mock.assert_called_once_with(application.id)


def test_clean_autofill_supported_but_below_score_threshold_enters_timed_queue_instead(db, settings):
    settings.min_score_for_auto_launch = 65
    db.commit()

    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    application = make_application(db, posting, match_score=50)

    with patch("app.services.autofill_service.launch_autofill_in_background") as launch_mock:
        result = confirmation_service.evaluate_and_enqueue(db, application.id)

    assert result.status == "Pending Confirmation"
    assert result.confirmation_deadline is not None
    launch_mock.assert_not_called()


def test_clean_unsupported_source_enters_timed_pending_confirmation(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="linkedin")
    application = make_application(db, posting)

    result = confirmation_service.evaluate_and_enqueue(db, application.id)

    assert result.status == "Pending Confirmation"
    assert result.confirmation_deadline is not None


def test_scam_flagged_posting_is_a_hard_stop_even_with_a_high_score(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="linkedin", scam_flag_reason="Off-platform payment request")
    application = make_application(db, posting, match_score=99)

    result = confirmation_service.evaluate_and_enqueue(db, application.id)

    assert result.status == "Needs Review"


def test_eligibility_flagged_posting_is_a_hard_stop_even_with_a_high_score(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse", eligibility_flag_reason="requires U.S. citizenship")
    application = make_application(db, posting, match_score=95)

    result = confirmation_service.evaluate_and_enqueue(db, application.id)

    assert result.status == "Needs Review"


def test_quiet_hours_pushes_a_deadline_that_would_land_inside_the_window(db, settings):
    settings.quiet_hours_enabled = True
    settings.quiet_hours_start_hour = 23
    settings.quiet_hours_end_hour = 7
    settings.local_timezone = "UTC"
    settings.confirmation_window_hours = 1.0
    db.commit()

    company = make_company(db)
    posting = make_posting(db, company, source="linkedin")
    application = make_application(db, posting, match_score=50)

    now = datetime(2026, 1, 1, 22, 30)  # 1 hour later lands at 23:30, inside quiet hours
    deadline, is_fast_track = confirmation_service.compute_confirmation_deadline(settings, application, now)

    assert is_fast_track is False
    local_hour = deadline.hour
    assert not confirmation_service._hour_in_range(local_hour, 23, 7)


def test_fast_track_requires_both_high_score_and_freshness(db, settings):
    settings.fast_track_score_threshold = 90
    settings.fast_track_freshness_minutes = 30
    settings.fast_track_window_hours = 2.0
    settings.confirmation_window_hours = 15.0
    settings.quiet_hours_enabled = False
    db.commit()

    company = make_company(db)
    now = utcnow()

    posting_fresh = make_posting(db, company, source="linkedin", first_seen_at=now)
    app_high_and_fresh = make_application(db, posting_fresh, match_score=95)
    _, fast_track = confirmation_service.compute_confirmation_deadline(settings, app_high_and_fresh, now)
    assert fast_track is True

    posting_stale = make_posting(db, company, source="linkedin", job_url="https://example.com/job/2", first_seen_at=now - timedelta(hours=2))
    app_high_but_stale = make_application(db, posting_stale, match_score=95)
    _, fast_track_stale = confirmation_service.compute_confirmation_deadline(settings, app_high_but_stale, now)
    assert fast_track_stale is False
