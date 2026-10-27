"""Simulated LLM usage for the public showcase instance.

The showcase runs with no API key, so its rule-based scoring, tailoring and outreach drafts make no LLM calls and the
dashboard's usage card would never move. On the showcase only, each of those actions writes the usage rows the same
action writes on a real instance: the number of calls it makes there, with token counts drawn from the spread measured
on the live personal deployment (claude-sonnet-4-6, 6,068 calls: input p10/p50/p90 = 2,327 / 3,969 / 13,932, output
9 / 437 / 1,875) and the cost from the provider's own pricing table. Rows are labelled "(simulated)" in the model name.
"""
import random

from ..app_mode import is_showcase_mode
from ..models import LlmUsageLog
from .llm.anthropic_provider import _PRICE_PER_MILLION_TOKENS

SIMULATED_MODEL = "claude-sonnet-4-6"

# Calls each action makes on a real instance, with (input, output) token ranges for each call.
_OPERATIONS = {
    # evaluate_match: profile + posting in, a short JSON verdict out.
    "score": [((3000, 6500), (300, 700))],
    # run_multi_pass_tailoring (tailor, ATS check, one refine pass), summary/skills, cover letter.
    "tailor": [((5000, 14000), (1200, 2400)), ((4000, 9000), (150, 400)), ((5000, 13000), (900, 1900)),
               ((2500, 5000), (300, 700)), ((3000, 6000), (450, 800))],
    # generate_interview_prep: process research, general + company prep, round outline, then Q&A per round (3 here).
    "interview_prep": [((3000, 6000), (500, 1200)), ((4000, 8000), (600, 1400)), ((3500, 7000), (400, 900)),
                       ((5000, 12000), (1200, 2400)), ((5000, 12000), (1200, 2400)), ((5000, 12000), (1200, 2400))],
    # mock_interview_service: the interviewer's next line, and the debrief (transcript + full profile in).
    "mock_turn": [((1500, 4000), (60, 200))],
    "mock_debrief": [((6000, 12000), (1200, 2600))],
    # _draft_note_text: a connection note or a short email.
    "outreach": [((1800, 3200), (80, 260))],
}


def record_simulated_run(db, operation: str, rng: random.Random | None = None) -> float:
    """Add the usage rows a real run of `operation` would add. Showcase only; returns the simulated cost in USD."""
    if not is_showcase_mode() or operation not in _OPERATIONS:
        return 0.0
    rng = rng or random.Random()
    prices = _PRICE_PER_MILLION_TOKENS[SIMULATED_MODEL]
    total = 0.0
    for (in_lo, in_hi), (out_lo, out_hi) in _OPERATIONS[operation]:
        inp, out = rng.randint(in_lo, in_hi), rng.randint(out_lo, out_hi)
        cost = round(inp / 1e6 * prices["input"] + out / 1e6 * prices["output"], 4)
        db.add(LlmUsageLog(provider="anthropic", model=f"{SIMULATED_MODEL} (simulated)", input_tokens=inp,
                           output_tokens=out, estimated_cost_usd=cost))
        total += cost
    return round(total, 4)
