"""Integration coverage for D1/D2/inverse wired into the real
tailor_application() pipeline -- resume_rules.py's own unit tests
cover the pure logic; this confirms the wiring actually flags a
violation end to end, with every other stage of the pipeline mocked
out (no real LLM calls)."""

import json
from unittest.mock import patch

from app.models import Company, JobApplication, JobPosting, ProfileVariant, ProfileVersion
from app.services import tailoring_service
from app.services.company_utils import normalize_company_name


def _application(db, profile_content):
    variant = ProfileVariant(name="Data Engineering", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    version = ProfileVersion(
        variant_id=variant.id, content_json=json.dumps(profile_content), source="manual", is_active=True,
    )
    db.add(version)
    db.commit()

    company = Company(name="Acme Corp", normalized_name=normalize_company_name("Acme Corp"))
    db.add(company)
    db.commit()
    db.refresh(company)
    posting = JobPosting(
        company_id=company.id, company_name_raw="Acme Corp", job_title="Data Engineer",
        job_description="Looking for a data engineer.", source="greenhouse",
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    application = JobApplication(posting_id=posting.id)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


_BASE_PROFILE = {
    "name": "Test Candidate",
    "summary": "Data engineer.",
    "skills": {},
    "experience": [{"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["Built pipelines."]}],
    "projects": [],
    "education": [],
    "certifications": [],
}


def _run_tailor_application(db, application, tailored_summary, tailored_bullets):
    with (
        patch(
            "app.services.tailoring_service.run_multi_pass_tailoring",
            return_value=(
                [{"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": tailored_bullets}],
                [], 90, [], [],
            ),
        ),
        patch("app.services.tailoring_service._tailor_summary_skills", return_value={"summary": tailored_summary, "skills": {}}),
        patch("app.services.tailoring_service.generate_cover_letter", return_value="A cover letter."),
        patch("app.services.tailoring_service.score_cover_letter", return_value=80),
    ):
        return tailoring_service.tailor_application(db, application.id)


class TestD1YearsClaimViolation:
    def test_inflated_years_claim_flags_attention_reason(self, db):
        application = _application(db, _BASE_PROFILE)
        # Real tenure: Jan-Jun 2024 = 6 months = 0 full years. Claiming
        # "5+ years" in the tailored summary should be caught.
        result = _run_tailor_application(db, application, "5+ years of experience.", ["Built pipelines."])
        assert result.attention_reason is not None
        assert "5+ years" in result.attention_reason

    def test_accurate_summary_does_not_flag(self, db):
        application = _application(db, _BASE_PROFILE)
        result = _run_tailor_application(db, application, "Experienced data engineer.", ["Built pipelines."])
        assert result.attention_reason is None


class TestD2UnverifiedPercentageViolation:
    def test_invented_percentage_flags_attention_reason(self, db):
        application = _application(db, _BASE_PROFILE)
        result = _run_tailor_application(db, application, "Data engineer.", ["Reduced latency by 47%."])
        assert result.attention_reason is not None
        assert "47%" in result.attention_reason

    def test_hedged_percentage_does_not_flag(self, db):
        application = _application(db, _BASE_PROFILE)
        result = _run_tailor_application(db, application, "Data engineer.", ["Reduced latency by roughly 47%."])
        assert result.attention_reason is None


class TestInverseSelfDeprecatingCheck:
    def test_self_deprecating_phrase_flags_attention_reason(self, db):
        application = _application(db, _BASE_PROFILE)
        result = _run_tailor_application(db, application, "Data engineer.", ["Only 3 months on this, but shipped it."])
        assert result.attention_reason is not None
        assert "only 3 months" in result.attention_reason.lower()
