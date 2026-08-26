"""Template rendering for the interview-prep platform feature build:
predicted interview rounds + process-research sources on the
application detail page, the confirmed story bank surfaced there, and
the draft/confirm/edit story bank UI on the Profile page. Mirrors the
render-only testing style already used in test_interview_prep_visibility.py
(a raw Jinja2 Environment, not the full app route) -- the LLM/Tavily
calls that produce this data aren't exercised here, same convention as
the rest of the suite."""

from jinja2 import Environment, FileSystemLoader

from app import models
from app.database import utcnow
from tests.conftest import make_application, make_company, make_posting, make_variant

env = Environment(loader=FileSystemLoader("app/templates"))


def _with_prep_row(db, application):
    prep = models.InterviewPrep(application_id=application.id)
    db.add(prep)
    db.commit()
    db.refresh(application)
    return application


def _base_detail_context(application, posting, **extra):
    context = {
        "application": application,
        "posting": posting,
        "match_analysis": None,
        "resume_doc": None,
        "cl_doc": None,
        "general_prep": None,
        "company_prep": None,
        "process_research": None,
        "predicted_rounds": None,
        "confirmed_stories": [],
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
    context.update(extra)
    return env.get_template("application_detail.html").render(**context)


def test_predicted_rounds_render_with_sources(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied", applied_at=utcnow())
    application = _with_prep_row(db, application)

    predicted_rounds = {
        "rounds": [
            {
                "round_name": "Recruiter screen",
                "what_it_tests": "background and motivation",
                "prep_focus": ["Walk through your resume", "Why this company"],
            }
        ],
        "grounded_in_real_research": True,
    }
    process_research = {"summary": "some findings", "sources": ["https://glassdoor.com/example"]}

    html = _base_detail_context(
        application, posting,
        predicted_rounds=predicted_rounds,
        process_research=process_research,
        general_prep={"likely_questions": ["Tell me about yourself."]},
    )

    assert "Predicted Interview Rounds" in html
    assert "Recruiter screen" in html
    assert "background and motivation" in html
    assert "Walk through your resume" in html
    assert "Based on real, reported interview experiences" in html
    assert "https://glassdoor.com/example" in html


def test_predicted_rounds_generic_fallback_labeled_as_such(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied", applied_at=utcnow())
    application = _with_prep_row(db, application)

    predicted_rounds = {
        "rounds": [{"round_name": "Technical screen", "what_it_tests": "coding basics", "prep_focus": []}],
        "grounded_in_real_research": False,
    }

    html = _base_detail_context(
        application, posting,
        predicted_rounds=predicted_rounds,
        general_prep={"likely_questions": ["x"]},
    )

    assert "No real process information was found" in html


def test_confirmed_story_bank_renders_on_application_detail(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied", applied_at=utcnow())
    application = _with_prep_row(db, application)

    class FakeStory:
        title = "Led a migration under a tight deadline"
        situation = "Legacy system was failing."
        task = "Own the migration."
        action = "Planned and executed a phased cutover."
        result = "Zero downtime, delivered a week early."
        traits = ["leadership", "ownership"]

    html = _base_detail_context(
        application, posting,
        confirmed_stories=[FakeStory()],
        general_prep={"likely_questions": ["x"]},
    )

    assert "Your Story Bank" in html
    assert "Led a migration under a tight deadline" in html
    assert "Zero downtime, delivered a week early." in html


def test_no_predicted_rounds_or_stories_omits_sections(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting, status="Applied", applied_at=utcnow())
    application = _with_prep_row(db, application)

    html = _base_detail_context(application, posting, general_prep={"likely_questions": ["x"]})

    assert "Predicted Interview Rounds" not in html
    assert "Your Story Bank" not in html


def _render_profile(variant_data, **extra):
    context = {
        "variant_data": variant_data,
        "message": None,
        "error": None,
        "csrf_token": "test-token",
        "static_version": "0",
        "is_authenticated": False,
    }
    context.update(extra)
    return env.get_template("profile.html").render(**context)


def _base_variant_entry(variant):
    return {
        "variant": variant,
        "active_version": None,
        "pending_versions": [],
        "versions": [],
        "eeo": {},
        "application_preferences": {},
        "education": [],
        "certifications": [],
        "completeness_warnings": [],
        "behavioral_stories": [],
    }


def test_profile_page_shows_draft_stories_prompt_when_empty(db):
    variant = make_variant(db)
    entry = _base_variant_entry(variant)

    html = _render_profile([entry])

    assert "Behavioral Story Bank (0)" in html
    assert "Draft Stories From Profile" in html
    assert "No stories drafted yet." in html


def test_profile_page_shows_draft_story_with_confirm_action(db):
    variant = make_variant(db)
    entry = _base_variant_entry(variant)

    class FakeStory:
        id = 1
        title = "Led a migration under a tight deadline"
        situation = "Legacy system was failing."
        task = "Own the migration."
        action = "Planned and executed a phased cutover."
        result = "Zero downtime, delivered a week early."
        traits = ["leadership"]
        status = "draft"
        source_reference = "CurioSync project"

    entry["behavioral_stories"] = [FakeStory()]

    html = _render_profile([entry])

    assert "Behavioral Story Bank (1)" in html
    assert "Led a migration under a tight deadline" in html
    assert "draft" in html
    assert "/profile/behavioral-stories/1/confirm" in html
    assert "Source: CurioSync project" in html


def test_profile_page_confirmed_story_hides_confirm_button(db):
    variant = make_variant(db)
    entry = _base_variant_entry(variant)

    class FakeStory:
        id = 2
        title = "Owned a client escalation"
        situation = "s"
        task = "t"
        action = "a"
        result = "r"
        traits = []
        status = "confirmed"
        source_reference = None

    entry["behavioral_stories"] = [FakeStory()]

    html = _render_profile([entry])

    assert "/profile/behavioral-stories/2/confirm" not in html
    assert "/profile/behavioral-stories/2/delete" in html
