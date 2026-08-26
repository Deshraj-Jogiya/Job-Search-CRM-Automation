"""Template rendering for the mock interview picker and live session
pages -- render-only tests, same style as test_interview_prep_platform.py."""

from jinja2 import Environment, FileSystemLoader

from app.services.mock_interview_service import TIER_DESCRIPTIONS
from tests.conftest import make_application, make_company, make_posting

env = Environment(loader=FileSystemLoader("app/templates"))


def _base_context(**extra):
    context = {
        "csrf_token": "test-token",
        "static_version": "0",
        "is_authenticated": False,
        "message": None,
        "error": None,
    }
    context.update(extra)
    return context


def test_mock_interview_home_renders_rounds_and_tiers(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    html = env.get_template("mock_interview_home.html").render(**_base_context(
        application=application, posting=posting,
        rounds=[{"round_name": "Recruiter Screen", "what_it_tests": "fit"}],
        sessions=[], tiers=TIER_DESCRIPTIONS,
    ))

    assert "Recruiter Screen" in html
    assert "Warm-Up" in html
    assert "Full Simulation" in html
    assert "Start Practice Session" in html


def test_mock_interview_home_handles_no_rounds(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    html = env.get_template("mock_interview_home.html").render(**_base_context(
        application=application, posting=posting, rounds=[], sessions=[], tiers=TIER_DESCRIPTIONS,
    ))

    assert "generate interview prep" in html.lower()


class FakeTurn:
    def __init__(self, speaker, content, is_followup=False, suggest_level_up=False, level_up_note=None):
        self.speaker = speaker
        self.content = content
        self.is_followup = is_followup
        self.suggest_level_up = suggest_level_up
        self.level_up_note = level_up_note


class FakeSession:
    def __init__(self, status="in_progress"):
        self.id = 1
        self.round_name = "Recruiter Screen"
        self.tier = "warm_up"
        self.status = status


def test_mock_interview_session_renders_transcript_in_progress(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    turns = [FakeTurn("interviewer", "Tell me about yourself.")]

    html = env.get_template("mock_interview_session.html").render(**_base_context(
        application=application, posting=posting, session=FakeSession(), turns=turns, debrief=None,
        tier_label="Warm-Up", tier_description="No time pressure.",
    ))

    assert "Tell me about yourself." in html
    assert "Submit Answer" in html
    assert "End Session" in html
    assert "<h3>Debrief</h3>" not in html


def test_mock_interview_session_shows_level_up_notice(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    turns = [
        FakeTurn("interviewer", "Tell me about yourself."),
        FakeTurn("candidate", "I'm a data engineer."),
        FakeTurn("interviewer", "Great follow-up.", is_followup=True, suggest_level_up=True, level_up_note="You're doing well."),
    ]

    html = env.get_template("mock_interview_session.html").render(**_base_context(
        application=application, posting=posting, session=FakeSession(), turns=turns, debrief=None,
        tier_label="Warm-Up", tier_description="No time pressure.",
    ))

    assert "You're doing well." in html
    assert "(follow-up)" in html


def test_mock_interview_session_shows_debrief_when_completed(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    turns = [FakeTurn("interviewer", "Tell me about yourself."), FakeTurn("candidate", "I'm a data engineer.")]
    debrief = {
        "overall_summary": "Solid session overall.",
        "strengths": ["Clear communication"],
        "areas_to_improve": ["Add more metrics"],
        "structure_feedback": "Good use of structure.",
        "accuracy_notes": [],
    }

    html = env.get_template("mock_interview_session.html").render(**_base_context(
        application=application, posting=posting, session=FakeSession(status="completed"), turns=turns,
        debrief=debrief, tier_label="Warm-Up", tier_description="No time pressure.",
    ))

    assert "Solid session overall." in html
    assert "Clear communication" in html
    assert "Add more metrics" in html
    assert "Submit Answer" not in html
