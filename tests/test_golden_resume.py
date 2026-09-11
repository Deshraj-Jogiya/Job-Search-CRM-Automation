"""Part F: golden reference test. Generates a resume from a fixed,
synthetic source profile (tests/fixtures/golden/sample_source_profile.json
-- a fictional candidate, never real personal data, see git log for
why) through the same C3/C4/C6 mechanical assembly
tailor_application() does after its LLM step (see
tailoring_service.py's resume_doc assembly block), and compares the
extracted text against a committed golden reference.

Deliberately skips the LLM bullet-rewrite step itself -- there's no
specific job description here to tailor against, and an LLM call
isn't reproducible run to run anyway. What this test actually proves
is that a real profile flows correctly through role classification
(C2), project selection (C6), skill filtering (C3), and metric hedging
(C4) into a correctly structured, ATS-safe .docx -- the same
deterministic pipeline every real generation goes through regardless
of what the LLM does to the bullet text.

Comparison is on EXTRACTED TEXT, not the raw .docx bytes -- python-docx
embeds a fresh timestamp/revision id in every file it writes, so a
byte-identical comparison would fail on every run even with zero
content change.

Stale golden: run `pytest tests/test_golden_resume.py --update-golden`
to regenerate both fixture files from current code. It prints a full
diff against the previous golden before overwriting -- never silently
auto-updates."""

import difflib
import json
from pathlib import Path

from app.services import docx_generator, resume_rules

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden"
_PROFILE_PATH = _FIXTURES_DIR / "sample_source_profile.json"
_DOCX_PATH = _FIXTURES_DIR / "sample_data_engineer.docx"
_EXPECTED_TXT_PATH = _FIXTURES_DIR / "sample_data_engineer.expected.txt"

_VARIANT_SLUG = "data_engineering"

# Self-contained -- NOT the real config/resume_rules.yaml default,
# which is tied to the live profile's own real project titles (see
# that file's own comment) and has nothing to do with this fixture's
# fictional projects. Everything else (skills/metrics/experience_
# classification bounds) still comes from the real config, since this
# test is meant to exercise the actual production rules end to end.
def _config_for_fixture():
    config = resume_rules.get_config()
    config = dict(config)
    config["projects_by_variant"] = dict(config["projects_by_variant"])
    config["projects_by_variant"]["variants"] = dict(config["projects_by_variant"]["variants"])
    config["projects_by_variant"]["variants"][_VARIANT_SLUG] = [
        "pipeline_cost_monitor", "retail_demand_forecasting_pipeline", "open_data_quality_toolkit",
    ]
    return config


def _build_resume_doc(profile: dict, config: dict) -> dict:
    """Mirrors tailoring_service.tailor_application()'s post-LLM
    assembly block (C6 selection, C3 filter, C4 hedge) -- minus the
    LLM rewrite itself, see module docstring."""
    candidate_projects = resume_rules.select_projects_for_variant(
        profile.get("projects", []), _VARIANT_SLUG, config
    )
    experience = profile.get("experience", [])
    summary = profile.get("summary", "")

    filtered_skills, _dropped = resume_rules.filter_skills(
        profile.get("skills", {}), experience, candidate_projects, summary, config
    )
    for entry in experience:
        entry["bullets"] = [resume_rules.hedge_unverified_metrics(b, config) for b in entry.get("bullets", [])]
    for project in candidate_projects:
        project["bullets"] = [resume_rules.hedge_unverified_metrics(b, config) for b in project.get("bullets", [])]

    return {
        "name": profile.get("name"),
        "title": profile.get("title"),
        "contact": profile.get("contact"),
        "summary": summary,
        "skills": filtered_skills,
        "experience": experience,
        "projects": candidate_projects,
        "education": profile.get("education", []),
        "certifications": profile.get("certifications", []),
    }


class TestGoldenResumeGeneration:
    def test_data_engineering_variant_matches_golden(self, update_golden):
        profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        config = _config_for_fixture()
        resume_doc = _build_resume_doc(profile, config)

        doc = docx_generator.build_resume_docx(resume_doc, config)
        actual_text = "\n".join(p.text for p in doc.paragraphs)

        if update_golden:
            previous = _EXPECTED_TXT_PATH.read_text(encoding="utf-8") if _EXPECTED_TXT_PATH.exists() else ""
            diff = "\n".join(difflib.unified_diff(
                previous.splitlines(), actual_text.splitlines(),
                fromfile="previous golden", tofile="new golden", lineterm="",
            ))
            print(f"\n--- golden diff ---\n{diff or '(no change)'}\n--- end diff ---")
            doc.save(_DOCX_PATH)
            _EXPECTED_TXT_PATH.write_text(actual_text, encoding="utf-8")
            return

        assert _EXPECTED_TXT_PATH.exists(), "No golden fixture yet -- run with --update-golden to create it."
        expected_text = _EXPECTED_TXT_PATH.read_text(encoding="utf-8")
        assert actual_text == expected_text

    def test_only_configured_projects_selected(self):
        profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        config = _config_for_fixture()
        resume_doc = _build_resume_doc(profile, config)

        names = {p["name"] for p in resume_doc["projects"]}
        assert names == {"Pipeline Cost Monitor", "Retail Demand Forecasting Pipeline", "Open Data Quality Toolkit"}
        assert "Home Energy Usage Tracker" not in names  # not in the variant's configured list

    def test_credential_entry_never_becomes_a_full_experience_entry(self):
        profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        config = _config_for_fixture()
        resume_doc = _build_resume_doc(profile, config)
        doc = docx_generator.build_resume_docx(resume_doc, config)
        text = "\n".join(p.text for p in doc.paragraphs)

        assert "Insight Data Bootcamp" in text
        assert "Data Engineering Fellow — Insight Data Bootcamp" not in text

    def test_concurrent_overlap_is_flagged(self):
        profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        config = _config_for_fixture()
        resume_doc = _build_resume_doc(profile, config)
        doc = docx_generator.build_resume_docx(resume_doc, config)
        text = "\n".join(p.text for p in doc.paragraphs)

        assert "(concurrent)" in text

    def test_negative_percentage_bullet_hedges_correctly(self):
        profile = json.loads(_PROFILE_PATH.read_text(encoding="utf-8"))
        config = _config_for_fixture()
        resume_doc = _build_resume_doc(profile, config)
        doc = docx_generator.build_resume_docx(resume_doc, config)
        text = "\n".join(p.text for p in doc.paragraphs)

        assert "roughly -15%" in text
