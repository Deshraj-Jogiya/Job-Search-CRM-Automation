"""
Phase 4b: audit, don't rebuild. The anti-fabrication safeguard
(_find_unsupported_keywords) already exists and is not touched here
except for one real, narrow gap this audit found and closed
(_verify_structural_fidelity, added in this same pass -- see
tailoring_service.py). Everything else below is adversarial testing
of what's ALREADY there, plus explicit documentation of what it still
doesn't catch.

FINDINGS (see each test class's docstring for the specific scenario):
1. CONFIRMED, FIXED: nothing mechanically verified the tailoring LLM's
   own prompt promise ("same roles, same order, same dates -- only
   bullets change") -- an LLM that renamed a company or shifted a date
   range would have sailed through unnoticed. Fixed by adding
   _verify_structural_fidelity(), wired into tailor_application()
   alongside the existing keyword check.
2. KNOWN GAP, NOT FIXED: an invented metric or outcome embedded
   directly inside a bullet's prose (e.g. "reduced latency by 40%"
   where the real bullet said nothing about a specific number) is NOT
   caught by either check. _find_unsupported_keywords only checks
   JD-extracted keywords that moved from "missing" to "resolved" --
   a fabricated number inside an otherwise-plausible bullet was never
   a "keyword" in that list to begin with, and _verify_structural_
   fidelity only checks company/role/date fields, not bullet prose.
   Building a real detector for this (distinguishing "genuinely
   rephrased from a real bullet" from "invented from nothing") is a
   harder problem than a quick mechanical patch can solve honestly --
   flagged here rather than attempted, per the instruction to fix only
   what these tests actually catch.
3. KNOWN GAP, NOT FIXED (same root cause as #2): an invented employer
   NAME appearing only inside bullet prose (not the structural
   `company` field) -- e.g. a bullet that says "collaborated with
   Google's infra team" when the candidate never worked with Google --
   isn't checked at all. Same reasoning as #2.
4. NOT A GAP: invented degree/certification claims are structurally
   impossible via this pipeline today -- tailor_application() copies
   `education`/`certifications` directly from profile_content with no
   LLM pass touching either field (see resume_doc construction). Verified
   directly in tests/test_tool_equivalence.py's sibling coverage; not
   re-tested here since there's no code path to adversarially test.
"""

from app.services.tailoring_service import _find_unsupported_keywords, _verify_structural_fidelity


_REAL_PROFILE = {
    "name": "Test Candidate",
    "skills": {"languages": ["Python", "SQL"], "tools": ["Airflow", "dbt"]},
    "experience": [
        {
            "role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present",
            "bullets": ["Built ETL pipelines using Airflow and dbt.", "Migrated a legacy warehouse to Snowflake."],
        },
        {
            "role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021",
            "bullets": ["Analyzed customer churn using SQL and Python."],
        },
    ],
    "projects": [{"name": "Real Project", "bullets": ["Used Kafka for streaming ingestion."], "technologies": ["Kafka"]}],
}


class TestUnsupportedTechnologyIsCaught:
    """A JD stuffed with technologies genuinely absent from the
    candidate's real bullet pool -- these must never slip through as
    "resolved"."""

    def test_completely_absent_technology_flagged(self):
        unsupported = _find_unsupported_keywords(_REAL_PROFILE, ["Kubernetes"])
        assert unsupported == ["Kubernetes"]

    def test_several_absent_technologies_all_flagged(self):
        fabricated = ["Kubernetes", "Terraform", "Rust", "GraphQL"]
        unsupported = _find_unsupported_keywords(_REAL_PROFILE, fabricated)
        assert set(unsupported) == set(fabricated)

    def test_real_technology_correctly_not_flagged(self):
        unsupported = _find_unsupported_keywords(_REAL_PROFILE, ["Airflow"])
        assert unsupported == []

    def test_mixed_real_and_fabricated_only_flags_fabricated(self):
        unsupported = _find_unsupported_keywords(_REAL_PROFILE, ["Airflow", "Kubernetes", "dbt", "Rust"])
        assert set(unsupported) == {"Kubernetes", "Rust"}


class TestStructuralFidelityCatchesCompanyAndDateFabrication:
    """Finding #1 above -- the fix this audit added. A tailoring pass
    that renamed a company, changed a role title, or shifted a date
    range must be caught even though it never touches the "keywords"
    list at all."""

    def test_company_rename_is_caught(self):
        tailored = [
            {"role": "Data Engineer", "company": "FAANG Company", "date": "Jan 2022 - Present", "bullets": []},
            {"role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021", "bullets": []},
        ]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert any("Acme Corp" in v and "FAANG Company" in v for v in violations)

    def test_date_range_extension_is_caught(self):
        tailored = [
            {"role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2020 - Present", "bullets": []},
            {"role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021", "bullets": []},
        ]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert any("date changed" in v for v in violations)

    def test_role_title_inflation_is_caught(self):
        tailored = [
            {"role": "Senior Staff Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present", "bullets": []},
            {"role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021", "bullets": []},
        ]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert any("role changed" in v for v in violations)

    def test_honest_bullet_only_changes_produce_no_violations(self):
        tailored = [
            {"role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present", "bullets": ["Rewritten bullet using dbt."]},
            {"role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021", "bullets": ["Rewritten bullet using SQL."]},
        ]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert violations == []

    def test_dropped_entry_is_caught(self):
        # An LLM that silently drops an entire role rather than just
        # rewriting bullets -- also a structural fidelity problem.
        tailored = [{"role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present", "bullets": []}]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert any("count changed" in v for v in violations)


class TestKnownGapsDocumented:
    """These tests exist to make the two known, unfixed gaps
    EXPLICIT and monitored -- if a future safeguard closes one, this
    test should start failing and get updated, not silently pass
    forever as if the gap were still open. Confirms today's real
    behavior: an invented metric or an invented employer name embedded
    only in bullet prose passes both checks clean."""

    def test_invented_metric_in_a_bullet_is_not_caught_today(self):
        # The real bullet never mentioned a number; this one invents
        # "reduced latency by 47%" wholesale. Neither the keyword
        # check (no keyword list involved) nor structural fidelity
        # (bullets aren't checked at all) catches this.
        tailored = [
            {
                "role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present",
                "bullets": ["Reduced pipeline latency by 47% using Airflow and dbt."],
            },
            {"role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021", "bullets": []},
        ]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert violations == []  # confirms the gap: no mechanism flags this today

    def test_invented_employer_name_in_bullet_prose_is_not_caught_today(self):
        tailored = [
            {
                "role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present",
                "bullets": ["Partnered directly with Google's infrastructure team on migration."],
            },
            {"role": "Data Analyst", "company": "Beta Inc", "date": "Jun 2020 - Dec 2021", "bullets": []},
        ]
        violations = _verify_structural_fidelity(_REAL_PROFILE["experience"], tailored)
        assert violations == []  # confirms the gap: no mechanism flags this today
        # Also not caught by the keyword-support check, since "Google"
        # was never a JD-derived keyword being resolved:
        unsupported = _find_unsupported_keywords(_REAL_PROFILE, ["Google"])
        assert unsupported == ["Google"]  # would only be caught IF it happened
        # to also appear as a literal resolved keyword -- not guaranteed.
