"""C5: a variant switch changes ONLY the header title line, experience
entry order, project list, and summary -- it must never change a
role's bullet text. This app's code never threads variant info into
the experience-tailoring LLM call at all (see tailoring_service.py --
run_multi_pass_tailoring's experience argument doesn't depend on
variant_slug, only candidate_projects does), so given the same LLM
output for the same JD+experience input, two variants must produce
byte-identical experience bullets. What this test cannot verify is
LLM call-to-call determinism itself (an LLM property, not this code's)
-- it mocks the LLM call identically for both variants, which is the
honest, controllable scope: proving this app's OWN code doesn't
diverge experience tailoring by variant, not proving the LLM is
deterministic across two real network calls."""

import json
from unittest.mock import patch

from app.models import Company, JobApplication, JobPosting, ProfileVariant, ProfileVersion, TailoredDocument
from app.services import tailoring_service
from app.services.company_utils import normalize_company_name

_SHARED_PROFILE = {
    "name": "Test Candidate",
    "summary": "Data engineer.",
    "skills": {},
    "experience": [
        {"role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present", "bullets": ["Built pipelines."]},
    ],
    "projects": [
        {"name": "Career Pilot", "bullets": ["Built a CRM."]},
        {"name": "Sales RFM Segmentation", "bullets": ["Segmented customers."]},
        {"name": "TalentVenue EventIntel", "bullets": ["Built an events tool."]},
        {"name": "Tax Anomaly Audit", "bullets": ["Flagged anomalies."]},
    ],
    "education": [],
    "certifications": [],
}

# The identical LLM output both variant runs will return for experience
# tailoring -- simulates a deterministic LLM, the honest scope this test
# can actually control (see module docstring).
_SAME_TAILORED_EXPERIENCE = [
    {"role": "Data Engineer", "company": "Acme Corp", "date": "Jan 2022 - Present", "bullets": ["Rewrote bullet identically."]}
]


def _application_for_variant(db, variant_name):
    variant = ProfileVariant(name=variant_name, is_default=False)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    version = ProfileVersion(
        variant_id=variant.id, content_json=json.dumps(_SHARED_PROFILE), source="manual", is_active=True,
    )
    db.add(version)
    db.commit()

    company = Company(name=f"Acme {variant_name}", normalized_name=normalize_company_name(f"Acme {variant_name}"))
    db.add(company)
    db.commit()
    db.refresh(company)
    posting = JobPosting(
        company_id=company.id, company_name_raw=company.name, job_title="Data Engineer",
        job_description="Looking for a data engineer.", source="greenhouse",
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    application = JobApplication(posting_id=posting.id, profile_variant_id=variant.id)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


def _tailor_and_get_resume_doc(db, application):
    with (
        patch(
            "app.services.tailoring_service.run_multi_pass_tailoring",
            side_effect=lambda experience, projects, jd_text: (
                _SAME_TAILORED_EXPERIENCE, projects, 90, [], [],
            ),
        ),
        patch("app.services.tailoring_service._tailor_summary_skills", return_value={"summary": "Data engineer.", "skills": {}}),
        patch("app.services.tailoring_service.generate_cover_letter", return_value="cl"),
        patch("app.services.tailoring_service.score_cover_letter", return_value=80),
    ):
        tailoring_service.tailor_application(db, application.id)

    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application.id, TailoredDocument.document_type == "resume")
        .first()
    )
    return json.loads(doc.content)


class TestVariantIsolation:
    def test_experience_bullets_are_byte_identical_across_variants(self, db):
        de_application = _application_for_variant(db, "Data Engineering")
        ml_application = _application_for_variant(db, "ML Engineering")

        de_doc = _tailor_and_get_resume_doc(db, de_application)
        ml_doc = _tailor_and_get_resume_doc(db, ml_application)

        assert de_doc["experience"] == ml_doc["experience"]

    def test_project_selection_differs_by_variant(self, db):
        de_application = _application_for_variant(db, "Data Engineering")
        analytics_application = _application_for_variant(db, "Analytics")

        de_doc = _tailor_and_get_resume_doc(db, de_application)
        analytics_doc = _tailor_and_get_resume_doc(db, analytics_application)

        de_names = {p["name"] for p in de_doc["projects"]}
        analytics_names = {p["name"] for p in analytics_doc["projects"]}
        assert de_names != analytics_names
