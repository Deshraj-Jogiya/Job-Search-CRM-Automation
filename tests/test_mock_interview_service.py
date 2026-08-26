"""Mock interview practice service -- pure logic + orchestration, LLM
calls mocked out (same convention as the rest of this suite). Covers
session lifecycle (start -> respond -> end), the question pool assembly
that keeps opening questions grounded in what already passed
check_answer_grounding, and that a level-up suggestion actually
persists on the turn it happened on rather than living only in memory."""

import json
from unittest.mock import patch

import pytest

from app import models
from app.services import mock_interview_service
from app.services.mock_interview_service import MockInterviewServiceError
from tests.conftest import make_application, make_company, make_posting, make_variant


def _make_prep(db, application, rounds=None):
    predicted_rounds = {
        "rounds": rounds if rounds is not None else [
            {
                "round_name": "Recruiter Screen",
                "what_it_tests": "fit",
                "likely_interviewer": "HR generalist",
                "qa_pairs": [{"question": "Tell me about yourself.", "draft_answer": "..."}],
                "other_possible_questions": ["Why this company?"],
            }
        ],
    }
    prep = models.InterviewPrep(application_id=application.id, predicted_rounds_json=json.dumps(predicted_rounds))
    db.add(prep)
    db.commit()
    return prep


def test_get_available_rounds_returns_empty_without_prep(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    assert mock_interview_service.get_available_rounds(db, application.id) == []


def test_get_available_rounds_lists_round_names(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)

    rounds = mock_interview_service.get_available_rounds(db, application.id)
    assert rounds == [{"round_name": "Recruiter Screen", "what_it_tests": "fit"}]


def test_start_session_raises_without_prep(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)

    with pytest.raises(MockInterviewServiceError):
        mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")


def test_start_session_raises_for_unknown_round(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)

    with pytest.raises(MockInterviewServiceError):
        mock_interview_service.start_session(db, application.id, "Nonexistent Round", "warm_up")


def test_start_session_raises_for_unknown_tier(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)

    with pytest.raises(MockInterviewServiceError):
        mock_interview_service.start_session(db, application.id, "Recruiter Screen", "expert_mode")


def test_start_session_picks_opening_question_from_pool(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)

    with patch("app.services.mock_interview_service.random.choice", side_effect=lambda pool: pool[0]):
        session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    assert session.status == "in_progress"
    assert session.tier == "warm_up"
    turns = list(session.turns)
    assert len(turns) == 1
    assert turns[0].speaker == "interviewer"
    assert turns[0].content in ("Tell me about yourself.", "Why this company?")


def test_submit_answer_records_both_turns_and_persists_level_up(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    fake_response = json.dumps({
        "next_line": "Great, tell me more about that project.",
        "is_followup": True,
        "suggest_level_up": True,
        "level_up_note": "Your answers have been detailed and confident.",
    })
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        mock_llm.return_value.complete_json.return_value = fake_response
        interviewer_turn = mock_interview_service.submit_answer(db, session.id, "I'm a data engineer with...")

    db.refresh(session)
    turns = list(session.turns)
    assert len(turns) == 3  # opening question, candidate answer, interviewer follow-up
    assert turns[1].speaker == "candidate"
    assert turns[1].content == "I'm a data engineer with..."
    assert turns[2].speaker == "interviewer"
    assert turns[2].is_followup is True
    assert turns[2].suggest_level_up is True
    assert turns[2].level_up_note == "Your answers have been detailed and confident."
    assert interviewer_turn.content == "Great, tell me more about that project."


def test_submit_answer_stores_voice_metrics_on_candidate_turn(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    fake_response = json.dumps({"next_line": "ok", "is_followup": False, "suggest_level_up": False, "level_up_note": ""})
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        mock_llm.return_value.complete_json.return_value = fake_response
        mock_interview_service.submit_answer(
            db, session.id, "spoken answer",
            voice_metrics={"duration_seconds": 12.5, "pause_count": 1, "longest_pause_seconds": 3.0},
        )

    db.refresh(session)
    candidate_turn = [t for t in session.turns if t.speaker == "candidate"][0]
    assert candidate_turn.recording_duration_seconds == 12.5
    assert candidate_turn.pause_count == 1
    assert candidate_turn.longest_pause_seconds == 3.0


def test_submit_answer_without_voice_metrics_leaves_fields_null(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    fake_response = json.dumps({"next_line": "ok", "is_followup": False, "suggest_level_up": False, "level_up_note": ""})
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        mock_llm.return_value.complete_json.return_value = fake_response
        mock_interview_service.submit_answer(db, session.id, "typed answer")

    db.refresh(session)
    candidate_turn = [t for t in session.turns if t.speaker == "candidate"][0]
    assert candidate_turn.recording_duration_seconds is None
    assert candidate_turn.pause_count is None


def test_submit_answer_raises_for_completed_session(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")
    session.status = "completed"
    db.commit()

    with pytest.raises(MockInterviewServiceError):
        mock_interview_service.submit_answer(db, session.id, "answer")


def test_end_session_requires_at_least_one_candidate_turn(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    with pytest.raises(MockInterviewServiceError):
        mock_interview_service.end_session(db, session.id)


def test_end_session_generates_debrief_and_completes(db):
    make_variant(db)
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    fake_qa_response = json.dumps({"next_line": "ok", "is_followup": False, "suggest_level_up": False, "level_up_note": ""})
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        mock_llm.return_value.complete_json.return_value = fake_qa_response
        mock_interview_service.submit_answer(db, session.id, "my answer")

    fake_debrief = json.dumps({
        "accuracy_notes": [],
        "strengths": ["Clear communication"],
        "areas_to_improve": ["More specific metrics"],
        "structure_feedback": "Good structure.",
        "overall_summary": "Solid first pass.",
    })
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        mock_llm.return_value.complete_json.return_value = fake_debrief
        result = mock_interview_service.end_session(db, session.id)

    assert result.status == "completed"
    assert result.ended_at is not None
    debrief = json.loads(result.debrief_json)
    assert debrief["overall_summary"] == "Solid first pass."


def test_end_session_is_idempotent_when_already_completed(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)
    session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")
    session.status = "completed"
    session.debrief_json = json.dumps({"overall_summary": "already done"})
    db.commit()

    result = mock_interview_service.end_session(db, session.id)
    assert json.loads(result.debrief_json)["overall_summary"] == "already done"


def test_list_sessions_orders_most_recent_first(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application, rounds=[
        {"round_name": "Round A", "qa_pairs": [{"question": "Q1"}], "other_possible_questions": []},
        {"round_name": "Round B", "qa_pairs": [{"question": "Q2"}], "other_possible_questions": []},
    ])

    session_a = mock_interview_service.start_session(db, application.id, "Round A", "warm_up")
    session_b = mock_interview_service.start_session(db, application.id, "Round B", "guided")

    sessions = mock_interview_service.list_sessions(db, application.id)
    assert [s.id for s in sessions] == [session_b.id, session_a.id]


def test_delivery_stats_counts_filler_words_and_timing():
    from app.services.mock_interview_service import _delivery_stats

    class FakeTurn:
        def __init__(self, speaker, content, created_at, recording_duration_seconds=None, pause_count=None, longest_pause_seconds=None):
            self.speaker = speaker
            self.content = content
            self.created_at = created_at
            self.recording_duration_seconds = recording_duration_seconds
            self.pause_count = pause_count
            self.longest_pause_seconds = longest_pause_seconds

    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    turns = [
        FakeTurn("interviewer", "Tell me about a project.", t0),
        FakeTurn("candidate", "Um, so, like, I built a pipeline, you know, with Spark.", t0 + timedelta(seconds=20)),
    ]

    stats = _delivery_stats(turns)
    assert "2 filler word(s)" in stats or "3 filler word(s)" in stats  # um, like, you know
    assert "~20s from question shown to answer submitted" in stats


def test_delivery_stats_ignores_stale_elapsed_time():
    from app.services.mock_interview_service import _delivery_stats

    class FakeTurn:
        def __init__(self, speaker, content, created_at, recording_duration_seconds=None, pause_count=None, longest_pause_seconds=None):
            self.speaker = speaker
            self.content = content
            self.created_at = created_at
            self.recording_duration_seconds = recording_duration_seconds
            self.pause_count = pause_count
            self.longest_pause_seconds = longest_pause_seconds

    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    turns = [
        FakeTurn("interviewer", "Question.", t0),
        FakeTurn("candidate", "Answer with no fillers.", t0 + timedelta(hours=5)),  # stale/resumed session
    ]

    stats = _delivery_stats(turns)
    assert "to respond" not in stats


def test_delivery_stats_uses_real_recording_duration_for_voice_answers():
    from app.services.mock_interview_service import _delivery_stats

    class FakeTurn:
        def __init__(self, speaker, content, created_at, recording_duration_seconds=None, pause_count=None, longest_pause_seconds=None):
            self.speaker = speaker
            self.content = content
            self.created_at = created_at
            self.recording_duration_seconds = recording_duration_seconds
            self.pause_count = pause_count
            self.longest_pause_seconds = longest_pause_seconds

    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    # 20 real words spoken over 10 actual recorded seconds -> 120 wpm,
    # even though the wall-clock gap between turns (which would include
    # think-time) is much longer -- the point of tracking real speech
    # duration instead of turn-to-turn elapsed time.
    turns = [
        FakeTurn("interviewer", "Question.", t0),
        FakeTurn(
            "candidate", "one two three four five six seven eight nine ten "
            "eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty",
            t0 + timedelta(seconds=90), recording_duration_seconds=10.0,
        ),
    ]

    stats = _delivery_stats(turns)
    assert "10s of actual recorded speech" in stats
    assert "120 words/min" in stats
    assert "from question shown to answer submitted" not in stats  # real duration takes priority over wall-clock


def test_delivery_stats_reports_pause_data_when_present():
    from app.services.mock_interview_service import _delivery_stats

    class FakeTurn:
        def __init__(self, speaker, content, created_at, recording_duration_seconds=None, pause_count=None, longest_pause_seconds=None):
            self.speaker = speaker
            self.content = content
            self.created_at = created_at
            self.recording_duration_seconds = recording_duration_seconds
            self.pause_count = pause_count
            self.longest_pause_seconds = longest_pause_seconds

    from datetime import datetime, timedelta
    t0 = datetime(2026, 1, 1, 12, 0, 0)
    turns = [
        FakeTurn("interviewer", "Question.", t0),
        FakeTurn(
            "candidate", "There was a long silence here.", t0 + timedelta(seconds=15),
            recording_duration_seconds=15.0, pause_count=2, longest_pause_seconds=4.2,
        ),
    ]

    stats = _delivery_stats(turns)
    assert "2 mid-answer pause(s)" in stats
    assert "longest ~4s" in stats


def test_find_previous_session_matches_same_round_only(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application, rounds=[
        {"round_name": "Round A", "qa_pairs": [{"question": "Q1"}], "other_possible_questions": []},
        {"round_name": "Round B", "qa_pairs": [{"question": "Q2"}], "other_possible_questions": []},
    ])

    old_a = mock_interview_service.start_session(db, application.id, "Round A", "warm_up")
    old_a.status = "completed"
    old_a.debrief_json = json.dumps({"scorecard": {"communication_clarity": 2}})
    other_round = mock_interview_service.start_session(db, application.id, "Round B", "warm_up")
    other_round.status = "completed"
    db.commit()

    new_a = mock_interview_service.start_session(db, application.id, "Round A", "guided")

    from app.services.mock_interview_service import _find_previous_session
    found = _find_previous_session(db, new_a)
    assert found.id == old_a.id


def test_find_previous_session_ignores_in_progress_sessions(db):
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)

    mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")
    new_session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")

    from app.services.mock_interview_service import _find_previous_session
    assert _find_previous_session(db, new_session) is None


def test_end_session_passes_comparison_and_visual_data_to_prompt(db):
    make_variant(db)
    company = make_company(db)
    posting = make_posting(db, company)
    application = make_application(db, posting)
    _make_prep(db, application)

    old_session = mock_interview_service.start_session(db, application.id, "Recruiter Screen", "warm_up")
    old_session.status = "completed"
    old_session.debrief_json = json.dumps({"scorecard": {"communication_clarity": 2, "content_accuracy": 3}})
    db.commit()

    new_session = mock_interview_service.start_session(
        db, application.id, "Recruiter Screen", "guided", camera_enabled=True
    )
    fake_qa = json.dumps({"next_line": "ok", "is_followup": False, "suggest_level_up": False, "level_up_note": ""})
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        mock_llm.return_value.complete_json.return_value = fake_qa
        mock_interview_service.submit_answer(db, new_session.id, "my real answer")

    fake_debrief = json.dumps({
        "accuracy_notes": [], "strengths": [], "areas_to_improve": [],
        "delivery_feedback": "ok", "scorecard": {"communication_clarity": 4},
        "comparison": {"has_previous": True, "trend": "improved", "note": "better", "warning": ""},
        "overall_summary": "Improved.",
    })
    captured_prompt = {}
    with patch("app.services.mock_interview_service.get_llm_provider") as mock_llm:
        def capture(system, prompt, **kwargs):
            captured_prompt["text"] = prompt
            return fake_debrief
        mock_llm.return_value.complete_json.side_effect = capture
        result = mock_interview_service.end_session(
            db, new_session.id, visual_metrics={"frames_analyzed": 100, "frames_face_forward": 60, "movement_events": 4}
        )

    assert "previous COMPLETED session" in captured_prompt["text"]
    assert "communication_clarity" in captured_prompt["text"]
    assert "Camera feedback was on" in captured_prompt["text"]
    assert "60%" in captured_prompt["text"]
    assert json.loads(result.debrief_json)["comparison"]["trend"] == "improved"
    assert json.loads(result.visual_metrics_json)["movement_events"] == 4
