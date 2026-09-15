"""A real ReAct-pattern (Reason + Act) agent loop over this app's own
data -- Thought / Action / Action Input / Observation cycles, repeated
until the model emits a Final Answer, with real tool-calling against
the real database (not a single one-shot LLM call, which is what
every other LLM usage in this codebase is -- see tailoring_service.py,
CurioSync's llm_service.py).

Standalone and additive: not wired into any existing route or
tailoring flow, so it can't change any current behavior. Takes an
injected `LLMProvider`-shaped object (anything with a
`complete_text(system, prompt) -> str` method) rather than importing
get_llm_provider() directly, so tests can drive the loop with a
scripted fake provider instead of a live API call -- the standard way
to unit-test agent-loop control flow without hitting a real LLM in CI.
"""

import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

from sqlalchemy.orm import Session

from .models_access import count_applications_by_status, search_company

MAX_STEPS = 6

_ACTION_RE = re.compile(r"Action:\s*(\w+)\s*\nAction Input:\s*(.+)", re.IGNORECASE)
_FINAL_ANSWER_RE = re.compile(r"Final Answer:\s*(.+)", re.IGNORECASE | re.DOTALL)

SYSTEM_PROMPT = """You are a research agent answering questions about this candidate's job search, \
using only the tools provided. Respond in exactly this format, one step at a time:

Thought: <your reasoning>
Action: <tool name>
Action Input: <input to the tool>

Wait for an Observation, then continue. When you have enough information, respond with:

Thought: <your reasoning>
Final Answer: <the answer>

Available tools:
- search_company(name): looks up a company's real status/sponsorship info by name.
- count_applications(status): counts real job applications by status (e.g. "applied", "rejected").
"""


class LLMLike(Protocol):
    def complete_text(self, system: str, prompt: str) -> str: ...


@dataclass
class AgentStep:
    thought: str | None = None
    action: str | None = None
    action_input: str | None = None
    observation: str | None = None
    final_answer: str | None = None


@dataclass
class AgentResult:
    final_answer: str
    steps: list[AgentStep] = field(default_factory=list)


TOOLS: dict[str, Callable[[Session, str], str]] = {
    "search_company": search_company,
    "count_applications": count_applications_by_status,
}


def run_research_agent(db: Session, llm: LLMLike, question: str, max_steps: int = MAX_STEPS) -> AgentResult:
    """Runs the real Thought/Action/Observation loop, calling real tools
    against the real database, until the model emits a Final Answer or
    max_steps is reached.

    Real bug found live 2026-09-15: asked "How many applications are
    marked applied?" against a real database where the true answer was
    1 (or 2, depending on definition -- checked directly), the model
    answered "7" on its very first response, having called zero tools --
    a confident, specific, completely ungrounded number. The system
    prompt only ever INSTRUCTED tool use; nothing MECHANICALLY enforced
    it, so the model skipping straight to a Final Answer worked exactly
    as the code allowed. used_a_real_tool below closes that: a Final
    Answer offered before any real tool has actually returned an
    Observation is rejected and the model is told why, same "never trust
    the LLM's self-report, mechanically verify" posture as every other
    LLM output in this codebase (see tailoring_service.py's fabrication
    checks). Calling an unknown/nonexistent tool does NOT count as a
    real tool use -- that's an error observation, not grounding."""
    transcript = f"Question: {question}\n"
    steps: list[AgentStep] = []
    used_a_real_tool = False

    for _ in range(max_steps):
        response = llm.complete_text(system=SYSTEM_PROMPT, prompt=transcript)
        transcript += response + "\n"

        final_match = _FINAL_ANSWER_RE.search(response)
        if final_match:
            if not used_a_real_tool:
                transcript += (
                    "SYSTEM: That Final Answer is rejected -- you have not called any real tool yet, "
                    "so there is nothing grounding it. Call a real tool (search_company or "
                    "count_applications) and wait for its Observation before answering.\n"
                )
                continue
            answer = final_match.group(1).strip()
            steps.append(AgentStep(final_answer=answer))
            return AgentResult(final_answer=answer, steps=steps)

        action_match = _ACTION_RE.search(response)
        if not action_match:
            # Model didn't follow the format -- stop rather than loop forever on garbage.
            return AgentResult(final_answer=response.strip(), steps=steps)

        tool_name, tool_input = action_match.group(1).strip(), action_match.group(2).strip()
        tool = TOOLS.get(tool_name)
        if tool:
            observation = tool(db, tool_input)
            used_a_real_tool = True
        else:
            observation = f"Unknown tool: {tool_name}"

        steps.append(AgentStep(action=tool_name, action_input=tool_input, observation=observation))
        transcript += f"Observation: {observation}\n"

    return AgentResult(final_answer="Agent did not reach a final answer within the step limit.", steps=steps)
