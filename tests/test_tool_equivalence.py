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


# Real false positive (2026-08-29): a JD-derived keyword bundling real
# tool names inside descriptive wording never matched the profile's own,
# differently-phrased skill entry, even with the exact same tools listed.
# "Terraform (Infrastructure as Code)", "Automated agent evaluation
# tooling (LangSmith/Opik/Langfuse)", and "CI/CD for data workflows" all
# got flagged as unsupported for a QuantumBlack tailoring run despite the
# real profile listing "Terraform (IaC)" and "LLMOps & Automated Agent
# Evaluation (LangSmith, Opik, Langfuse)" verbatim elsewhere. Fixed by
# also checking each atomic term inside the keyword (parenthetical
# contents, comma/slash-separated pieces) against the profile, not just
# the keyword's exact full-phrase text or a curated equivalence group.

def test_keyword_with_real_tool_name_before_parenthetical_is_supported():
    profile = {"skills": {"devops": ["Terraform (IaC)"]}}
    result = _find_unsupported_keywords(profile, ["Terraform (Infrastructure as Code)"])
    assert result == []


def test_keyword_bundling_several_real_tools_in_parens_is_supported():
    profile = {"skills": {"llmops": ["LLMOps & Automated Agent Evaluation (LangSmith, Opik, Langfuse)"]}}
    result = _find_unsupported_keywords(
        profile, ["Automated agent evaluation tooling (LangSmith/Opik/Langfuse)"]
    )
    assert result == []


def test_keyword_with_no_real_matching_atomic_term_still_flagged():
    profile = {"skills": {"devops": ["Docker", "Kubernetes"]}}
    result = _find_unsupported_keywords(profile, ["Infrastructure automation (Pulumi/Chef)"])
    assert result == ["Infrastructure automation (Pulumi/Chef)"]


# Real false positive, flagged directly by the candidate: a real bullet
# describing presenting/reporting real work ("presented the analysis to
# stakeholders", "submitted the report") routinely never names a word
# processor or slide tool by brand at all -- so a JD keyword like
# "Microsoft Word" or "PowerPoint" was getting flagged as unsupported
# fabrication even though writing/presenting real work trivially implies
# using SOME tool in this category. Fixed by excluding generic office/
# presentation tool names from the fabrication check entirely, not just
# grouping them (grouping alone would still require the profile to name
# some tool in the group, which these accomplishment-style bullets don't).

def test_generic_word_processor_keyword_is_never_flagged_even_with_no_profile_mention():
    profile = {"experience": [{"bullets": ["Presented the quarterly data analysis to senior stakeholders."]}]}
    assert _find_unsupported_keywords(profile, ["Microsoft Word"]) == []


def test_generic_presentation_tool_keyword_is_never_flagged():
    profile = {"experience": [{"bullets": ["Submitted a written report summarizing key findings."]}]}
    assert _find_unsupported_keywords(profile, ["PowerPoint"]) == []


def test_generic_office_suite_keyword_is_never_flagged():
    profile = {"skills": {"languages": ["Python", "SQL"]}}
    assert _find_unsupported_keywords(profile, ["Google Suite"]) == []


def test_generic_tool_exclusion_does_not_false_match_unrelated_real_terms():
    # "word" and "docs" are real substrings of unrelated technical terms
    # -- must not silently excuse a genuinely unsupported claim just
    # because it happens to contain one of these short words.
    profile = {"skills": {"languages": ["Python"]}}
    assert _find_unsupported_keywords(profile, ["Keyword extraction pipeline"]) == ["Keyword extraction pipeline"]
    assert _find_unsupported_keywords(profile, ["Docstring generation with LLMs"]) == ["Docstring generation with LLMs"]


def test_spreadsheet_tools_are_deliberately_not_excused():
    # Excel/Sheets represent real, distinct data-manipulation skill --
    # unlike Word/PowerPoint, still requires real evidence in the profile.
    profile = {"experience": [{"bullets": ["Presented the quarterly data analysis to senior stakeholders."]}]}
    assert _find_unsupported_keywords(profile, ["Excel"]) == ["Excel"]


def test_real_technical_fabrication_still_flagged_alongside_generic_tools():
    profile = {"experience": [{"bullets": ["Presented findings to stakeholders using PowerPoint."]}]}
    result = _find_unsupported_keywords(profile, ["PowerPoint", "NetSuite integration"])
    assert result == ["NetSuite integration"]
