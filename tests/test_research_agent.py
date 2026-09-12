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


def test_agent_handles_an_unknown_tool_gracefully(db):
    llm = ScriptedLLM([
        "Thought: trying a made-up tool.\nAction: fly_to_the_moon\nAction Input: now",
        "Thought: that didn't work.\nFinal Answer: I couldn't complete that request.",
    ])

    result = run_research_agent(db, llm, "Do something unsupported")

    assert "Unknown tool" in result.steps[0].observation
    assert result.final_answer == "I couldn't complete that request."


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
