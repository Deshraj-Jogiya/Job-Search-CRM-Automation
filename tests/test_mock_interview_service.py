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
