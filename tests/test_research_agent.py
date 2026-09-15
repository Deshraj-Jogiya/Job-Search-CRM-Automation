"""Tests the real ReAct loop control flow (parsing Action/Action Input,
invoking the real tool against a real database, feeding the
Observation back in, terminating on Final Answer) by driving it with a
scripted fake LLM -- the standard way to unit-test an agent loop
without a live API call."""

from app.models import Company
from app.services.research_agent import run_research_agent


class ScriptedLLM:
    """Returns each response in `script` in order, one per call --
    stands in for a real LLMProvider."""

    def __init__(self, script: list[str]):
        self.script = list(script)
        self.calls: list[tuple[str, str]] = []

    def complete_text(self, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return self.script.pop(0)


def test_agent_calls_the_real_tool_and_uses_the_real_observation(db):
    db.add(Company(name="Acme Corp", normalized_name="acme corp", status="Blocked", status_reason="Ghosted twice"))
    db.commit()

    llm = ScriptedLLM([
        "Thought: I should look up Acme Corp.\nAction: search_company\nAction Input: Acme Corp",
        "Thought: I now know the status.\nFinal Answer: Acme Corp is Blocked because they ghosted twice.",
    ])

    result = run_research_agent(db, llm, "What's the status of Acme Corp?")

    assert result.final_answer == "Acme Corp is Blocked because they ghosted twice."
    assert len(result.steps) == 2
    assert result.steps[0].action == "search_company"
    assert "Blocked" in result.steps[0].observation
    # the real observation must have been fed back into the next prompt
    assert "Blocked" in llm.calls[1][1]


def test_agent_stops_after_max_steps_without_a_final_answer(db):
    llm = ScriptedLLM([
        "Thought: still thinking.\nAction: count_applications\nAction Input: applied",
        "Thought: still thinking.\nAction: count_applications\nAction Input: applied",
    ])

    result = run_research_agent(db, llm, question="How many applications?", max_steps=2)

    assert "did not reach a final answer" in result.final_answer
    assert len(result.steps) == 2


def test_agent_handles_an_unknown_tool_gracefully_then_recovers_with_a_real_one(db):
    # An unknown-tool attempt does NOT count as grounding -- see the
    # ungrounded-final-answer tests below for why. If the model tries a
    # bad tool and then actually calls a real one, that real Observation
    # is what allows the Final Answer through.
    db.add(Company(name="Acme Corp", normalized_name="acme corp", status="Active", status_reason=None))
    db.commit()

    llm = ScriptedLLM([
        "Thought: trying a made-up tool.\nAction: fly_to_the_moon\nAction Input: now",
        "Thought: let me use a real tool instead.\nAction: search_company\nAction Input: Acme Corp",
        "Thought: now I know.\nFinal Answer: Acme Corp is Active.",
    ])

    result = run_research_agent(db, llm, "Do something unsupported")

    assert "Unknown tool" in result.steps[0].observation
    assert result.steps[1].action == "search_company"
    assert result.final_answer == "Acme Corp is Active."


class TestUngroundedFinalAnswerRejected:
    """Real bug found live 2026-09-15: asked "How many applications are
    marked applied?" against a real database where the true count was 1
    (or 2, depending on definition -- checked directly), the model
    answered "7" on its very first response having called zero tools --
    a confident, specific, completely fabricated number. The system
    prompt only instructed tool use; nothing mechanically enforced it."""

    def test_final_answer_before_any_tool_call_is_rejected_not_trusted(self, db):
        llm = ScriptedLLM([
            # Exactly what happened live: a Final Answer with no prior
            # tool call at all.
            "Thought: I think I know this.\nFinal Answer: 7 applications.",
            "Thought: let me actually check.\nAction: count_applications\nAction Input: applied",
            "Thought: now I have the real number.\nFinal Answer: 1 application(s) with status 'applied'.",
        ])

        result = run_research_agent(db, llm, "How many applications are marked applied?")

        # The fabricated "7" must never reach the final answer.
        assert result.final_answer == "1 application(s) with status 'applied'."
        assert any(s.action == "count_applications" for s in result.steps)

    def test_repeated_ungrounded_final_answers_fall_through_honestly(self, db):
        # If the model never manages a real tool call within budget, the
        # existing honest "did not reach a final answer" fallback must
        # win -- never a fabricated number, even under a tight step limit.
        llm = ScriptedLLM([
            "Thought: guessing.\nFinal Answer: 7 applications.",
            "Thought: guessing again.\nFinal Answer: 12 applications.",
        ])

        result = run_research_agent(db, llm, "How many applications?", max_steps=2)

        assert "did not reach a final answer" in result.final_answer
        assert "7" not in result.final_answer
        assert "12" not in result.final_answer


def test_count_applications_tool_reflects_real_database_state(db):
    from app.models import JobApplication, JobPosting, Company

    company = Company(name="Test Co", normalized_name="test co")
    db.add(company)
    db.commit()
    db.refresh(company)

    posting = JobPosting(company_id=company.id, company_name_raw="Test Co", job_title="Engineer", job_description="d", source="test")
    db.add(posting)
    db.commit()
    db.refresh(posting)

    db.add(JobApplication(posting_id=posting.id, status="applied"))
    db.commit()

    llm = ScriptedLLM([
        "Thought: check the count.\nAction: count_applications\nAction Input: applied",
        "Thought: got it.\nFinal Answer: There is 1 applied application.",
    ])
    result = run_research_agent(db, llm, "How many applications are marked applied?")

    assert "1 application(s) with status 'applied'" in result.steps[0].observation
