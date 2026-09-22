"""Real end-to-end render + redirect checks for the merged Review Queue /
Ready to Apply page (review.html), same pattern as
test_kanban_page_template.py. Built 2026-09-22 after explicit user
direction to toggle these two together (a real linked pair -- Review
clears things INTO Approved, Ready to Apply is where Approved things go
next) while keeping Daily Triage and List/Kanban as separate pages."""

from unittest.mock import patch

from jinja2 import Environment, FileSystemLoader
from starlette.requests import Request

from app.routers import jobs as jobs_router
from tests.conftest import make_application, make_company, make_posting

env = Environment(loader=FileSystemLoader("app/templates"))


def _render(**extra):
    context = {
        "pending": [], "needs_review": [], "ready_to_apply_rows": [],
        "message": None, "error": None,
        "csrf_token": "test-csrf-token-abc123",
        "static_version": "0", "is_authenticated": False,
    }
    context.update(extra)
    return env.get_template("review.html").render(**context)


def test_both_tab_buttons_are_present(db):
    html = _render()
    assert 'data-tab="review"' in html
    assert 'data-tab="ready-to-apply"' in html


def test_ready_to_apply_row_renders_the_real_posting_link(db):
    company = make_company(db)
    posting = make_posting(db, company, job_title="Data Engineer II", job_url="https://boards.greenhouse.io/x/1")
    application = make_application(db, posting, status="Approved")

    html = _render(ready_to_apply_rows=[(application, posting)])

    assert "Data Engineer II" in html
    assert 'href="https://boards.greenhouse.io/x/1"' in html


def test_needs_review_row_still_renders_in_its_own_panel(db):
    company = make_company(db)
    posting = make_posting(db, company, job_title="Fraud Analyst")
    application = make_application(db, posting, status="Needs Review", attention_reason="Possible fabrication")

    html = _render(needs_review=[application])

    review_panel = html[html.index('data-tab-panel="review"'):html.index('data-tab-panel="ready-to-apply"')]
    assert "Fraud Analyst" in review_panel
    assert "Possible fabrication" in review_panel


def _fake_request(query_string: str = "") -> Request:
    scope = {"type": "http", "method": "GET", "path": "/jobs/ready-to-apply", "query_string": query_string.encode(), "headers": []}
    return Request(scope)


def test_old_ready_to_apply_url_redirects_to_the_review_page_with_the_right_tab(db):
    response = jobs_router.ready_to_apply_page(_fake_request())
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/review?tab=ready-to-apply"


def test_ready_to_apply_redirect_preserves_other_query_params(db):
    response = jobs_router.ready_to_apply_page(_fake_request("message=Marked+Applied."))
    assert response.status_code == 303
    assert "tab=ready-to-apply" in response.headers["location"]
    assert "message=Marked" in response.headers["location"]


def test_review_page_route_passes_all_three_datasets_to_the_template(db):
    company = make_company(db)
    posting = make_posting(db, company)
    make_application(db, posting, status="Approved")

    with patch("app.routers.jobs.render") as render_mock:
        jobs_router.review_page(_fake_request(), db)

    context = render_mock.call_args[0][2]
    assert "pending" in context
    assert "needs_review" in context
    assert "ready_to_apply_rows" in context
    assert len(context["ready_to_apply_rows"]) == 1
