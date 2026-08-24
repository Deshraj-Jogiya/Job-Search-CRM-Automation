"""Submission auto-detection. The detection heuristic itself
(_looks_like_submission_confirmation) is pure and tested directly; the
watch loop is tested against a fake Playwright Page so it never opens a
real browser."""

import pytest

from app.services import autofill_service
from app.services.autofill_service import (
    _auto_mark_applied,
    _looks_like_submission_confirmation,
    _watch_for_submission_and_close,
)
from tests.conftest import make_application, make_company, make_posting


class FakePage:
    """A scripted sequence of (url, text, is_closed) snapshots, one
    consumed per is_closed()/url/inner_text() cycle -- lets a test drive
    the watch loop through several polls without a real browser or
    real waiting."""

    def __init__(self, steps):
        self._steps = list(steps)
        self._index = 0
        self.wait_calls = 0

    def _current(self):
        idx = min(self._index, len(self._steps) - 1)
        return self._steps[idx]

    def is_closed(self):
        return self._current()["closed"]

    @property
    def url(self):
        return self._current()["url"]

    def inner_text(self, _selector):
        return self._current()["text"]

    def wait_for_timeout(self, _ms):
        self.wait_calls += 1
        self._index += 1


def test_url_change_to_confirmation_path_is_detected():
    assert _looks_like_submission_confirmation(
        current_url="https://boards.greenhouse.io/acme/jobs/123/thank-you",
        current_text="",
        baseline_url="https://boards.greenhouse.io/acme/jobs/123",
        baseline_text="",
    )


def test_new_confirmation_phrase_in_text_is_detected():
    assert _looks_like_submission_confirmation(
        current_url="https://jobs.lever.co/acme/abc/apply",
        current_text="thank you for applying to acme corp",
        baseline_url="https://jobs.lever.co/acme/abc/apply",
        baseline_text="apply for this job at acme corp",
    )


def test_phrase_already_present_in_baseline_does_not_false_positive():
    """A JD that happens to mention 'we have received your application'
    materials in its own boilerplate (e.g. describing the hiring
    process) must not trigger a false positive just because the phrase
    is present -- only NEW appearance of it counts."""
    text = "once we have received your application, our team will review it within a week"
    assert not _looks_like_submission_confirmation(
        current_url="https://jobs.ashbyhq.com/acme/def",
        current_text=text,
        baseline_url="https://jobs.ashbyhq.com/acme/def",
        baseline_text=text,
    )


def test_unrelated_navigation_is_not_a_false_positive():
    assert not _looks_like_submission_confirmation(
        current_url="https://boards.greenhouse.io/acme/jobs/123?ref=linkedin",
        current_text="fill out the form below to apply",
        baseline_url="https://boards.greenhouse.io/acme/jobs/123",
        baseline_text="fill out the form below to apply",
    )


def test_watch_loop_marks_applied_once_confirmation_appears(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    application = make_application(db, posting, status="Approved")

    page = FakePage(
        [
            {"url": "https://boards.greenhouse.io/acme/jobs/1", "text": "apply now", "closed": False},
            {"url": "https://boards.greenhouse.io/acme/jobs/1", "text": "apply now", "closed": False},
            {"url": "https://boards.greenhouse.io/acme/jobs/1/thank-you", "text": "thank you for applying", "closed": False},
            {"url": "https://boards.greenhouse.io/acme/jobs/1/thank-you", "text": "thank you for applying", "closed": True},
        ]
    )

    _watch_for_submission_and_close(db, application.id, page)

    db.refresh(application)
    assert application.status == "Applied"
    assert application.notes and "Auto-detected" in application.notes


def test_watch_loop_leaves_application_untouched_if_never_confirmed(db, settings):
    company = make_company(db)
    posting = make_posting(db, company, source="lever")
    application = make_application(db, posting, status="Approved")

    page = FakePage(
        [
            {"url": "https://jobs.lever.co/acme/1/apply", "text": "apply now", "closed": False},
            {"url": "https://jobs.lever.co/acme/1/apply", "text": "apply now", "closed": True},
        ]
    )

    _watch_for_submission_and_close(db, application.id, page)

    db.refresh(application)
    assert application.status == "Approved"


def test_auto_mark_applied_skips_gracefully_if_already_applied(db, settings):
    """Covers the race where the human clicks the manual 'Mark as
    Applied' button while the watcher is still polling -- the watcher's
    own mark_applied call should fail closed (log, don't crash the
    background thread)."""
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    application = make_application(db, posting, status="Applied")

    _auto_mark_applied(db, application.id, "https://boards.greenhouse.io/acme/jobs/1/thank-you")

    db.refresh(application)
    assert application.status == "Applied"
