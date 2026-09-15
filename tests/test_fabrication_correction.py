"""Integration coverage for the fabrication self-correction loop in
tailor_application() -- added because every real fabrication finding
used to go straight to a human, every single time, which defeats
automating this at all (the user's own words: "makes things harder
than manual tailoring"). The checks themselves (_find_unsupported_
keywords, check_bullet_fabrication) are exactly as strict as before;
this confirms a real finding now gets a bounded, targeted correction
attempt first, re-verified with the SAME real checks, before ever
reaching a human -- and that it still falls through to the existing
Needs-Review flag when correction genuinely can't produce something
clean."""

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
        job_description="Looking for a data engineer with Kubernetes experience.", source="greenhouse",
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

_FABRICATED_EXPERIENCE = [
    {"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["Deployed on Kubernetes clusters."]}
]
_CORRECTED_EXPERIENCE = [
    {"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["Built pipelines."]}
]


def _patched_tailoring(initial_missing, remaining_missing_first, verify_side_effect=None, correction_side_effect=None):
    """Common patch set: run_multi_pass_tailoring reports "Kubernetes"
    as resolved on the first pass (present in initial_missing, absent
    from remaining), which _find_unsupported_keywords will flag since
    the real profile never mentions it. verify_side_effect/
    correction_side_effect drive what happens on each correction
    attempt inside the loop."""
    patches = [
        patch(
            "app.services.tailoring_service.run_multi_pass_tailoring",
            return_value=(_FABRICATED_EXPERIENCE, [], 90, initial_missing, remaining_missing_first),
        ),
        patch(
            "app.services.tailoring_service._tailor_summary_skills",
            return_value={"summary": "Data engineer.", "skills": {}},
        ),
        patch("app.services.tailoring_service.generate_cover_letter", return_value="A cover letter."),
        patch("app.services.tailoring_service.score_cover_letter", return_value=80),
        patch("app.services.tailoring_service.check_bullet_fabrication", return_value=[]),
    ]
    if verify_side_effect is not None:
        patches.append(patch("app.services.tailoring_service._verify_ats_score", side_effect=verify_side_effect))
    if correction_side_effect is not None:
        patches.append(patch("app.services.tailoring_service._correct_fabricated_content_pass", side_effect=correction_side_effect))
    return patches


def _apply_all(patches):
    for p in patches:
        p.start()
    return patches


def _stop_all(patches):
    for p in patches:
        p.stop()


class TestCorrectionSucceeds:
    def test_clean_correction_clears_attention_reason_and_logs_info(self, db):
        application = _application(db, _BASE_PROFILE)
        patches = _patched_tailoring(
            initial_missing=["Kubernetes"],
            remaining_missing_first=[],  # "resolved" on the first pass -- unsupported, real profile never mentions it
            # After correction, the re-verify pass honestly reports
            # Kubernetes as still missing (the correction removed the
            # false claim instead of just rewording it) -- so it's no
            # longer in "resolved_keywords" and _find_unsupported_keywords
            # comes back clean.
            verify_side_effect=[{"score": 85, "missing_keywords": ["Kubernetes"]}],
            correction_side_effect=[{"experience": _CORRECTED_EXPERIENCE, "projects": []}],
        )
        _apply_all(patches)
        try:
            result = tailoring_service.tailor_application(db, application.id)
        finally:
            _stop_all(patches)

        assert result.attention_reason is None


class TestCorrectionGivesUpAfterMaxPasses:
    def test_still_unsupported_after_max_passes_falls_through_to_needs_review(self, db):
        application = _application(db, _BASE_PROFILE)
        # Every re-verify still reports Kubernetes as resolved (the
        # correction pass never actually removes the claim in this
        # scenario) -- exhausts _MAX_FABRICATION_CORRECTION_PASSES (2)
        # and must fall through to the existing hard-stop flag, not
        # loop forever or silently accept the unresolved fabrication.
        patches = _patched_tailoring(
            initial_missing=["Kubernetes"],
            remaining_missing_first=[],
            verify_side_effect=[
                {"score": 85, "missing_keywords": []},
                {"score": 85, "missing_keywords": []},
            ],
            correction_side_effect=[
                {"experience": _FABRICATED_EXPERIENCE, "projects": []},
                {"experience": _FABRICATED_EXPERIENCE, "projects": []},
            ],
        )
        _apply_all(patches)
        try:
            result = tailoring_service.tailor_application(db, application.id)
        finally:
            _stop_all(patches)

        assert result.attention_reason is not None
        assert "Kubernetes" in result.attention_reason

    def test_correction_llm_call_failing_falls_through_immediately(self, db):
        application = _application(db, _BASE_PROFILE)
        patches = _patched_tailoring(
            initial_missing=["Kubernetes"],
            remaining_missing_first=[],
            correction_side_effect=RuntimeError("LLM call failed"),
        )
        _apply_all(patches)
        try:
            result = tailoring_service.tailor_application(db, application.id)
        finally:
            _stop_all(patches)

        assert result.attention_reason is not None
        assert "Kubernetes" in result.attention_reason


class TestCorrectionPassBreakingStructureStopsCorrecting:
    def test_structural_break_during_correction_flags_structural_not_fabrication(self, db):
        application = _application(db, _BASE_PROFILE)
        # The correction pass itself returns a different entry count --
        # a worse problem than the one it was trying to fix. Must stop
        # attempting further correction and flag the structural issue,
        # not keep looping or silently accept a broken structure.
        broken_structure = [
            {"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["Built pipelines."]},
            {"role": "Extra Invented Role", "company": "Fake Co", "date": "Jan 2020 - Jan 2021", "bullets": ["Invented."]},
        ]
        patches = _patched_tailoring(
            initial_missing=["Kubernetes"],
            remaining_missing_first=[],
            correction_side_effect=[{"experience": broken_structure, "projects": []}],
        )
        _apply_all(patches)
        try:
            result = tailoring_service.tailor_application(db, application.id)
        finally:
            _stop_all(patches)

        assert result.attention_reason is not None
        assert "structural" in result.attention_reason.lower()
