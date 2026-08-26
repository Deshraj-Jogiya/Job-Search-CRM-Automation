"""Interview Prep visibility on application_detail.html: previously the
whole section vanished the moment an application hit a closed status
(Rejected/Not Selected), silently discarding any prep you'd already
generated and read. Now closed-with-existing-prep stays visible
(read-only, no Regenerate button -- no point re-researching a dead
lead); closed-with-nothing-generated stays hidden (no point inviting
fresh research either). Also covers the Jobs list's new "interview prep
ready" badge (app/routers/jobs.py's joinedload + jobs.html)."""

import json

from jinja2 import Environment, FileSystemLoader

from app import models
from app.database import utcnow
from tests.conftest import make_application, make_company, make_posting

env = Environment(loader=FileSystemLoader("app/templates"))


def _render_detail(application, posting, **extra_context):
    context = {
        "application": application,
        "posting": posting,
        "match_analysis": None,
        "resume_doc": None,
        "cl_doc": None,
        "general_prep": None,
        "company_prep": None,
        "interview_prep": application.interview_prep,
        "outreach_messages": [],
        "daily_outreach_cap": 10,
        "outreach_sent_today": 0,
        "discovery_available": False,
        "discovered_contacts": None,
        "autofill_supported": False,
        "autofill_supported_sources": [],
        "message": None,
        "error": None,
        "csrf_token": "test-token",
        "static_version": "0",
        "is_authenticated": False,
    }
    context.update(extra_context)
    return env.get_template("application_detail.html").render(**context)


def _with_prep(db, application):
    general_prep = {"strengths_to_emphasize": ["Tell me about yourself."]}
    prep = models.InterviewPrep(
        application_id=application.id,
        general_prep_json=json.dumps(general_prep),
    )
    db.add(prep)
    db.commit()
    db.refresh(application)
    return {"general_prep": general_prep, "interview_prep": application.interview_prep}


def test_open_application_with_no_prep_shows_generate_prompt(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied", applied_at=utcnow())

    html = _render_detail(application, posting)

    assert "Generate Interview Prep" in html
    assert "Not generated yet" in html


def test_open_application_with_prep_shows_regenerate(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied", applied_at=utcnow())
    extra = _with_prep(db, application)

    html = _render_detail(application, posting, **extra)

    assert "Regenerate" in html
    assert "Tell me about yourself." in html


def test_rejected_application_with_no_prep_hides_section_entirely(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Rejected", rejected_at=utcnow())

    html = _render_detail(application, posting)

    assert "Interview Prep" not in html


def test_rejected_application_with_prep_stays_visible_read_only(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Rejected", rejected_at=utcnow())
    extra = _with_prep(db, application)

    html = _render_detail(application, posting, **extra)

    assert "Interview Prep" in html
    assert "Tell me about yourself." in html
    assert "This application is closed" in html
    assert "Generate Interview Prep" not in html
    assert "Regenerate" not in html


def test_jobs_list_shows_interview_prep_ready_badge(db):
    from app.routers import jobs as jobs_router

    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied")
    _with_prep(db, application)

    applications = (
        db.query(models.JobApplication)
        .join(models.JobPosting)
        .options(jobs_router.joinedload(models.JobApplication.interview_prep))
        .all()
    )
    html = env.get_template("jobs.html").render(
        applications=applications,
        sources=[],
        keywords=[],
        seniority_exclusions=[],
        location_exclusions=[],
        target_companies=[],
        automation_enabled=False,
        message=None,
        error=None,
        csrf_token="test-token",
        static_version="0",
        is_authenticated=False,
    )

    assert "interview prep ready" in html
