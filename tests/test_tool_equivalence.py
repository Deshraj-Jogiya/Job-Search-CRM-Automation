"""_find_unsupported_keywords is the mechanical, non-LLM fabrication
check behind tailoring_service's attention_reason flag -- the safety
net that routes a tailored application to Needs Review instead of
auto-launching. It's pure logic with no LLM/DB dependency, so it's
covered directly here rather than only via a live tailoring run.

Covers the tool-equivalence fix (2026-08-23): a keyword naming a
specific tool should count as supported if the profile shows hands-on
experience with a directly comparable tool in the same narrow category
(e.g. Tableau supports a "Power BI" keyword), without opening the door
to crediting genuinely unrelated tools/skills.
"""

from app.services.tailoring_service import _find_unsupported_keywords


def test_literal_match_still_supported():
    profile = {"skills": {"languages": ["Python", "SQL"]}}
    assert _find_unsupported_keywords(profile, ["Python"]) == []


def test_equivalent_tool_in_same_category_is_supported():
    profile = {"skills": {"bi_tools": ["Tableau"]}}
    assert _find_unsupported_keywords(profile, ["Power BI"]) == []


def test_equivalent_tool_is_bidirectional():
    profile = {"skills": {"orchestration": ["Apache Airflow"]}}
    assert _find_unsupported_keywords(profile, ["Dagster"]) == []


def test_unrelated_tool_still_flagged_as_unsupported():
    profile = {"skills": {"bi_tools": ["Tableau"]}}
    assert _find_unsupported_keywords(profile, ["Kubernetes"]) == ["Kubernetes"]


def test_unrelated_keyword_outside_any_equivalence_group_still_flagged():
    profile = {"skills": {"languages": ["Python"]}}
    assert _find_unsupported_keywords(profile, ["Rust"]) == ["Rust"]


def test_mixed_batch_only_flags_the_genuinely_unsupported_ones():
    profile = {"skills": {"bi_tools": ["Tableau"], "languages": ["Python"]}}
    result = _find_unsupported_keywords(profile, ["Power BI", "Python", "Kafka"])
    assert result == ["Kafka"]


# Real false positive (2026-08-27): a verbose JD-derived keyword phrase
# never exact-matched a group member, even when the profile plainly had
# the equivalent real skill under different vocabulary -- Kolmogorov-
# Smirnov tests/Population Stability Index work got flagged as
# unsupported for a "statistics and experimentation (A/B testing,
# hypothesis testing)" JD phrase. Fixed to substring-match a group term
# INSIDE the keyword phrase, not require the whole phrase to equal one.

def test_verbose_jd_phrase_containing_a_group_term_is_supported():
    profile = {
        "projects": [
            {
                "name": "AI Model Observability & Fairness Audits",
                "bullets": ["Audits ML models by running Kolmogorov-Smirnov (KS) tests and Population Stability Index (PSI) to monitor feature drift."],
            }
        ]
    }
    result = _find_unsupported_keywords(
        profile, ["statistics and experimentation (A/B testing, hypothesis testing)"]
    )
    assert result == []


def test_verbose_jd_phrase_still_flagged_when_genuinely_unsupported():
    profile = {"skills": {"languages": ["Python", "SQL"]}}
    result = _find_unsupported_keywords(
        profile, ["statistics and experimentation (A/B testing, hypothesis testing)"]
    )
    assert result == ["statistics and experimentation (A/B testing, hypothesis testing)"]


def test_exact_single_term_group_matches_still_work_after_the_substring_change():
    profile = {"skills": {"bi_tools": ["Tableau"]}}
    assert _find_unsupported_keywords(profile, ["Power BI"]) == []
