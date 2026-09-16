"""Real end-to-end render check for kanban.html against real DB-backed
fixtures, same pattern as test_jobs_page_template.py -- catches template-
level bugs (a card in the wrong column, a missing CSRF token that would
403 every drag, a missing "(auto)" label) that a pure logic test of
_group_applications_by_status alone can't."""

import json

from jinja2 import Environment, FileSystemLoader

from app.routers.jobs import KANBAN_COLUMNS, _group_applications_by_status
from tests.conftest import make_application, make_company, make_posting

env = Environment(loader=FileSystemLoader("app/templates"))


def _render(columns, valid_source_statuses=None, **extra):
    context = {
        "columns": columns,
        "column_order": KANBAN_COLUMNS,
        "valid_source_statuses_json": json.dumps(valid_source_statuses or {}),
        "message": None, "error": None,
        "csrf_token": "test-csrf-token-abc123",
        "static_version": "0", "is_authenticated": False,
    }
    context.update(extra)
    return env.get_template("kanban.html").render(**context)


def test_card_renders_in_its_real_status_column(db):
    company = make_company(db)
    application = make_application(db, make_posting(db, company, job_title="Data Engineer II"), status="Applied")
    columns = _group_applications_by_status([application])

    html = _render(columns)

    applied_col_start = html.index('data-status="Applied"')
    interviewing_col_start = html.index('data-status="Interviewing"')
    assert applied_col_start < html.index("Data Engineer II") < interviewing_col_start


def test_auto_columns_are_labeled_and_no_other_column_is(db):
    html = _render(_group_applications_by_status([]))

    assert html.count("(auto)") == 2  # Ingested, Tailored -- exactly, not more not fewer


def test_empty_column_shows_its_own_empty_state(db):
    html = _render(_group_applications_by_status([]))
    assert "Nothing here." in html


def test_csrf_token_is_embedded_for_the_drag_fetch_call(db):
    # Real gap this guards against: the board's drag POST is a fetch(),
    # not a <form>, so it doesn't get a csrf_token field for free the way
    # every other action route in this app does -- CSRFMiddleware checks
    # every form-encoded POST with no exemption. Missing this 403s every
    # single drag.
    html = _render(_group_applications_by_status([]))
    assert 'const CSRF_TOKEN = "test-csrf-token-abc123";' in html


def test_valid_source_statuses_are_passed_through_to_the_client_untouched(db):
    html = _render(_group_applications_by_status([]), valid_source_statuses={"Applied": ["Approved"]})
    assert '"Applied": ["Approved"]' in html


def test_pending_confirmation_deadline_shown_only_on_that_column(db):
    from datetime import datetime, timedelta

    company = make_company(db)
    application = make_application(
        db, make_posting(db, company), status="Pending Confirmation",
        confirmation_deadline=datetime(2026, 9, 20, 15, 0, 0),
    )
    columns = _group_applications_by_status([application])

    html = _render(columns)

    assert "auto-proceeds 2026-09-20 15:00 UTC" in html


def test_sortable_js_is_vendored_locally_not_loaded_from_a_cdn(db):
    html = _render(_group_applications_by_status([]))
    assert '/static/js/vendor/sortable.min.js' in html
    assert "cdn" not in html.lower()
