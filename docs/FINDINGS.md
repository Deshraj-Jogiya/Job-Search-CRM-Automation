# Findings

Measured and tested results from this repository. Every number below comes from a tracked file in the repository, named under each
finding.

## What does LLM tailoring cost per application on a live deployment?

**How it was tested:** every LLM call the app makes is written to the `llm_usage_logs` table with its token counts and cost; the
totals below were read from the live personal deployment on 2026-09-15 (the dashboard's "Real LLM Cost" card shows the running total).

| Measure | Value |
|---|---|
| Total LLM cost | $2.72 |
| LLM calls | 82 |
| Applications | 32 |
| Average per application | about $0.085 |
| Model | Claude Sonnet 4.6 (the code default) |

**Decision:** Claude stays the default for tailoring and the fabrication checks, and the whole system also runs for $0 on Gemini's
free tier or a local Ollama model (`LLM_PROVIDER`). A daily spend cap (`daily_llm_spend_cap_usd`) is checked before every metered call.

**Skills shown:** LLM cost logging per call, provider-agnostic LLM layer (Anthropic, Gemini, Ollama), FastAPI dashboard, SQLAlchemy.

**Source:** `README.md` ("Running cost"), `app/services/llm/`, `app/models.py` (`LlmUsageLog`).

## Can an AI fit score inflate a job of the wrong type?

**How it was tested:** `tests/test_fit_service.py` scores a Data Analyst posting against a software-engineer profile while the AI
match score is mocked at 88. The rule-based fit (`app/services/fit_service.py`: 60% skill overlap, 20% role type, 20% years asked
for) detects that the posting's role type is not one the profile targets or has held.

| Measure | Value |
|---|---|
| AI score before the cap | 88 |
| Score stored and used | 40 (`ROLE_MISMATCH_CAP`) |
| Cap applied | before fast-track, auto-tailor or autofill read the score |
| Tailoring afterwards | does not lift the cap (`test_tailoring_does_not_lift_the_cap`) |

**Decision:** every posting shows the rule-based fit next to the AI score, each requirement with where it appears in the profile,
and a job of the wrong type cannot score above 40 under either.

**Skills shown:** rule-based scoring, pytest with mocked LLM calls, SQLAlchemy, explainable scoring.

**Source:** `tests/test_fit_service.py` (`test_an_inflated_ai_score_is_capped_before_anything_reads_it`), `app/services/fit_service.py`
(module docstring, item 2).

## Does the anti-fabrication safeguard catch invented content?

**How it was tested:** an adversarial audit of the tailoring safeguard (`tests/test_tailoring_adversarial.py`, 11 tests): technologies
absent from the profile, renamed companies or shifted dates, invented metrics inside bullet text, invented employer names in bullet
text, and invented degrees or certificates.

| Scenario | Result |
|---|---|
| Technology absent from the profile | caught (`_find_unsupported_keywords`) |
| Renamed company or shifted dates | gap found and fixed (`_verify_structural_fidelity`) |
| Invented metric inside a bullet | gap found and fixed (`check_bullet_fabrication`) |
| Invented employer name inside a bullet | gap found and fixed (same check) |
| Invented degree or certificate | not possible: those sections are copied from the profile with no LLM pass |

**Decision:** tailored output is checked by plain code against the master profile before it can go out
(`docs/decisions/0002-non-llm-fabrication-guardrail.md`); the bullet check is an LLM judgement and is documented as imperfect.

**Skills shown:** adversarial testing, pytest, mechanical guardrails around LLM output.

**Source:** `tests/test_tailoring_adversarial.py` (findings 1-4 in its docstring), `docs/decisions/0002-non-llm-fabrication-guardrail.md`.
