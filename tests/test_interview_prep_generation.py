"""Pure-logic pieces of interview prep generation that don't need a real
LLM/Tavily call: the mechanical fabrication safeguard on drafted answers
(check_answer_grounding, same posture as profile_service.detect_profile_
regressions but applied to something a candidate might say out loud),
and the round-assembly orchestration (_generate_predicted_rounds) with
its two LLM-calling helpers mocked out -- this is exactly the wiring
that broke three times in real testing before being fixed, and had zero
coverage until now."""

from unittest.mock import patch

from app.services.interview_prep_service import (
    _generate_predicted_rounds,
    _strip_confidential_projects,
    check_answer_grounding,
)


def test_check_answer_grounding_flags_uncited_metric():
    profile = {"experience": [{"bullets": ["Cut latency by 65%."]}]}
    rounds = {
        "rounds": [
            {
                "round_name": "Technical Screen",
                "qa_pairs": [{"question": "Tell me about a pipeline you built.", "draft_answer": "It reduced cost by 80%."}],
            }
        ]
    }
    warnings = check_answer_grounding(profile, rounds)
    assert len(warnings) == 1
    assert "80%" in warnings[0]
    assert "Technical Screen" in warnings[0]


def test_check_answer_grounding_allows_real_metric():
    profile = {"experience": [{"bullets": ["Cut latency by 65%."]}]}
    rounds = {
        "rounds": [
            {
                "round_name": "Technical Screen",
                "qa_pairs": [{"question": "Tell me about a pipeline.", "draft_answer": "I cut latency by 65% there."}],
            }
        ]
    }
    assert check_answer_grounding(profile, rounds) == []


def test_check_answer_grounding_ignores_answers_with_no_metrics():
    profile = {"experience": []}
    rounds = {
        "rounds": [{"round_name": "Behavioral", "qa_pairs": [{"question": "Tell me about a challenge.", "draft_answer": "It was a difficult project with a tight deadline."}]}]
    }
    assert check_answer_grounding(profile, rounds) == []


def test_check_answer_grounding_handles_empty_rounds():
    assert check_answer_grounding({}, {"rounds": []}) == []
    assert check_answer_grounding({}, {}) == []


def test_generate_predicted_rounds_assembles_qa_into_each_round():
    structure = {
        "rounds": [
            {"round_name": "Recruiter Screen", "likely_interviewer": "HR generalist"},
            {"round_name": "Technical Round", "likely_interviewer": "senior engineer"},
        ],
        "grounded_in_real_research": False,
    }

    def fake_qa(round_info, *args, **kwargs):
        return {
            "qa_pairs": [{"question": f"Q for {round_info['round_name']}", "draft_answer": "answer"}],
            "other_possible_questions": ["extra question"],
            "questions_to_ask_them": ["what's next?"],
        }

    with patch("app.services.interview_prep_service._generate_round_structure", return_value=structure):
        with patch("app.services.interview_prep_service._generate_round_qa", side_effect=fake_qa):
            result = _generate_predicted_rounds(
                "Data Engineer", "Acme", "JD text", {"summary": ""}, {"experience": []}, [], [], 8,
            )

    assert len(result["rounds"]) == 2
    assert result["rounds"][0]["round_name"] == "Recruiter Screen"
    assert result["rounds"][0]["qa_pairs"][0]["question"] == "Q for Recruiter Screen"
    assert result["rounds"][1]["qa_pairs"][0]["question"] == "Q for Technical Round"
    assert result["rounds"][0]["other_possible_questions"] == ["extra question"]
    assert result["rounds"][0]["questions_to_ask_them"] == ["what's next?"]


def test_generate_predicted_rounds_preserves_order_under_concurrency():
    # Rounds run in parallel (ThreadPoolExecutor) -- this asserts the
    # zip-by-index reassembly keeps results aligned to their own round
    # even though completion order isn't guaranteed.
    structure = {"rounds": [{"round_name": f"Round {i}"} for i in range(6)]}

    def fake_qa(round_info, *args, **kwargs):
        return {"qa_pairs": [{"question": round_info["round_name"]}], "other_possible_questions": [], "questions_to_ask_them": []}

    with patch("app.services.interview_prep_service._generate_round_structure", return_value=structure):
        with patch("app.services.interview_prep_service._generate_round_qa", side_effect=fake_qa):
            result = _generate_predicted_rounds("Role", "Co", "JD", {}, {}, [], [], 8)

    for i, round_ in enumerate(result["rounds"]):
        assert round_["qa_pairs"][0]["question"] == f"Round {i}"


def test_strip_confidential_projects_removes_flagged_entries():
    profile = {
        "projects": [
            {"name": "Personal Solo Project", "bullets": ["Built X."]},
            {"name": "Team NDA Project", "bullets": ["Contributed to Y."], "confidential": True},
        ],
        "experience": [{"title": "Engineer"}],
    }
    result = _strip_confidential_projects(profile)
    names = [p["name"] for p in result["projects"]]
    assert names == ["Personal Solo Project"]
    assert result["experience"] == [{"title": "Engineer"}]


def test_strip_confidential_projects_no_op_when_none_flagged():
    profile = {"projects": [{"name": "A"}, {"name": "B"}]}
    result = _strip_confidential_projects(profile)
    assert result == profile


def test_strip_confidential_projects_handles_missing_projects_key():
    assert _strip_confidential_projects({"experience": []}) == {"experience": []}
