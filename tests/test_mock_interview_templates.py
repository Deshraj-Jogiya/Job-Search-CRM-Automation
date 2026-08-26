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


def test_mock_interview_home_shows_camera_toggle_with_explanation(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    html = env.get_template("mock_interview_home.html").render(**_base_context(
        application=application, posting=posting,
        rounds=[{"round_name": "Live Coding", "what_it_tests": "coding"}],
        sessions=[], tiers=TIER_DESCRIPTIONS,
    ))

    assert "Enable camera feedback" in html
    assert "Nothing is recorded, uploaded" in html
    assert "phone screen" in html.lower()


class FakeSessionRow:
    def __init__(self, trend=None, status="completed"):
        self.id = 1
        self.round_name = "Recruiter Screen"
        self.tier = "warm_up"
        self.status = status
        self.started_at = __import__("datetime").datetime(2026, 1, 1, 12, 0, 0)
        self.trend = trend


def test_mock_interview_home_shows_improved_trend_badge(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    html = env.get_template("mock_interview_home.html").render(**_base_context(
        application=application, posting=posting, rounds=[],
        sessions=[FakeSessionRow(trend="improved")], tiers=TIER_DESCRIPTIONS,
    ))

    assert "improved" in html


def test_mock_interview_home_shows_declined_trend_badge(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    html = env.get_template("mock_interview_home.html").render(**_base_context(
        application=application, posting=posting, rounds=[],
        sessions=[FakeSessionRow(trend="declined")], tiers=TIER_DESCRIPTIONS,
    ))

    assert "declined" in html


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
        self.camera_enabled = False
        self.visual_metrics_json = None


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
        "areas_to_improve": [
            {
                "issue": "Missing metrics",
                "what_you_said": "I built a pipeline.",
                "why_it_matters": "Interviewers want quantified impact.",
                "example_better_answer": "I built a pipeline that cut latency by 40%.",
            }
        ],
        "delivery_feedback": "Good pace, no filler words.",
        "scorecard": {"communication_clarity": 4, "content_accuracy": 5},
        "comparison": {"has_previous": False, "trend": "n/a", "note": "", "warning": ""},
        "accuracy_notes": [],
    }

    html = env.get_template("mock_interview_session.html").render(**_base_context(
        application=application, posting=posting, session=FakeSession(status="completed"), turns=turns,
        debrief=debrief, visual_metrics={}, tier_label="Warm-Up", tier_description="No time pressure.",
    ))

    assert "Solid session overall." in html
    assert "Clear communication" in html
    assert "Missing metrics" in html
    assert "I built a pipeline that cut latency by 40%." in html
    assert "Good pace, no filler words." in html
    assert "Communication clarity: 4/5" in html
    assert "Submit Answer" not in html


def test_mock_interview_session_shows_decline_warning(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    turns = [FakeTurn("interviewer", "Q"), FakeTurn("candidate", "A")]
    debrief = {
        "overall_summary": "Rougher than last time.",
        "strengths": [],
        "areas_to_improve": [],
        "delivery_feedback": "",
        "scorecard": {"communication_clarity": 2},
        "comparison": {
            "has_previous": True, "trend": "declined",
            "note": "Clarity dropped versus your last attempt.",
            "warning": "Your directness score fell -- you dodged a direct question again.",
        },
        "accuracy_notes": [],
    }

    html = env.get_template("mock_interview_session.html").render(**_base_context(
        application=application, posting=posting, session=FakeSession(status="completed"), turns=turns,
        debrief=debrief, visual_metrics={}, tier_label="Warm-Up", tier_description="No time pressure.",
    ))

    assert "flash-error" in html
    assert "Clarity dropped versus your last attempt." in html
    assert "you dodged a direct question again." in html
