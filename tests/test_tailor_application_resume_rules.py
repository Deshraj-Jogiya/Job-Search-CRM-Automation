"""Integration coverage for C3 (skill filtering), C4 (metric hedging),
and C6 (config-driven project selection per variant) wired into the
real tailor_application() pipeline."""

import json
from unittest.mock import patch

from app.models import Company, JobApplication, JobPosting, ProfileVariant, ProfileVersion, TailoredDocument
from app.services import tailoring_service
from app.services.company_utils import normalize_company_name


def _application(db, profile_content, variant_name="Data Engineering"):
    variant = ProfileVariant(name=variant_name, is_default=True)
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


def _run_tailor_application(db, application, tailored_experience, tailored_projects, tailored_summary="Data engineer.", tailored_skills=None):
    with (
        patch(
            "app.services.tailoring_service.run_multi_pass_tailoring",
            return_value=(tailored_experience, tailored_projects, 90, [], []),
        ),
        patch(
            "app.services.tailoring_service._tailor_summary_skills",
            return_value={"summary": tailored_summary, "skills": tailored_skills or {}},
        ),
        patch("app.services.tailoring_service.generate_cover_letter", return_value="A cover letter."),
        patch("app.services.tailoring_service.score_cover_letter", return_value=80),
    ):
        tailoring_service.tailor_application(db, application.id)

    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application.id, TailoredDocument.document_type == "resume")
        .first()
    )
    return json.loads(doc.content)


class TestSkillFilteringWired:
    def test_skill_not_in_any_bullet_is_dropped_from_saved_document(self, db):
        profile = {
            "name": "Test", "summary": "d", "skills": {}, "experience": [], "projects": [],
            "education": [], "certifications": [],
        }
        application = _application(db, profile)
        experience = [{"role": "DE", "company": "X", "date": "Jan 2024 - Present", "bullets": ["Built pipelines with Airflow."]}]
        saved = _run_tailor_application(
            db, application, experience, [],
            tailored_skills={"Tools": ["Airflow", "Rust"]},
        )
        assert saved["skills"] == {"Tools": ["Airflow"]}


class TestMetricHedgingWired:
    def test_unverified_percentage_is_hedged_in_saved_document(self, db):
        # experience has one placeholder entry matching the mocked
        # tailored output below (role/company/date all identical) --
        # deliberately NOT empty like other tests in this file, since an
        # entry-count mismatch there would trip the real, unrelated
        # structural-fidelity check and mask what this test actually
        # means to isolate: percentage-hedging behavior alone.
        profile = {
            "name": "Test", "summary": "d", "skills": {}, "projects": [],
            "experience": [{"role": "DE", "company": "X", "date": "Jan 2024 - Present", "bullets": []}],
            "education": [], "certifications": [],
        }
        application = _application(db, profile)
        # 77% is deliberately not in the real config's verified_metrics
        # allowlist (unlike 40%, which is one of Deshraj's actual
        # confirmed-real bullet percentages as of 2026-09-11) -- this
        # test needs a genuinely unverified claim to exercise hedging.
        experience = [{"role": "DE", "company": "X", "date": "Jan 2024 - Present", "bullets": ["Cut latency by 77%."]}]
        saved = _run_tailor_application(db, application, experience, [])
        assert "roughly 77%" in saved["experience"][0]["bullets"][0]
        # Deliberately NOT a hard stop (2026-09-15) -- the saved document
        # is already safely hedged above, so this doesn't block automation
        # on top of that; it's still logged as a soft, non-blocking note
        # (see tailor_application's own "Soft note" log_activity call).
        application_row = db.query(JobApplication).filter(JobApplication.id == application.id).first()
        assert application_row.attention_reason is None


class TestAiClicheLanguageWired:
    def test_cliche_wording_rewritten_in_saved_resume_and_not_flagged(self, db):
        profile = {
            "name": "Test", "summary": "d", "skills": {}, "projects": [],
            "experience": [{"role": "DE", "company": "X", "date": "Jan 2024 - Present", "bullets": []}],
            "education": [], "certifications": [],
        }
        application = _application(db, profile)
        experience = [{
            "role": "DE", "company": "X", "date": "Jan 2024 - Present",
            "bullets": ["Leveraged Airflow to seamlessly automate reporting."],
        }]
        saved = _run_tailor_application(db, application, experience, [])
        bullet = saved["experience"][0]["bullets"][0]
        assert "Leveraged" not in bullet
        assert "seamlessly" not in bullet
        assert bullet == "Used Airflow to smoothly automate reporting."

        application_row = db.query(JobApplication).filter(JobApplication.id == application.id).first()
        assert application_row.attention_reason is None

    def test_cover_letter_also_gets_cliche_cleanup(self, db):
        profile = {
            "name": "Test", "summary": "d", "skills": {}, "experience": [], "projects": [],
            "education": [], "certifications": [],
        }
        application = _application(db, profile)
        with (
            patch(
                "app.services.tailoring_service.run_multi_pass_tailoring",
                return_value=([], [], 90, [], []),
            ),
            patch(
                "app.services.tailoring_service._tailor_summary_skills",
                return_value={"summary": "d", "skills": {}},
            ),
            patch(
                "app.services.tailoring_service.generate_cover_letter",
                return_value="I utilized my background to deliver results.",
            ),
            patch("app.services.tailoring_service.score_cover_letter", return_value=80),
        ):
            tailoring_service.tailor_application(db, application.id)

        doc = (
            db.query(TailoredDocument)
            .filter(TailoredDocument.application_id == application.id, TailoredDocument.document_type == "cover_letter")
            .first()
        )
        assert doc.content == "I used my background to deliver results."


class TestWeakBulletOpenerWired:
    def test_weak_opener_is_logged_as_a_soft_note_not_a_hard_stop(self, db):
        profile = {
            "name": "Test", "summary": "d", "skills": {}, "projects": [],
            "experience": [{"role": "DE", "company": "X", "date": "Jan 2024 - Present", "bullets": []}],
            "education": [], "certifications": [],
        }
        application = _application(db, profile)
        experience = [{
            "role": "DE", "company": "X", "date": "Jan 2024 - Present",
            "bullets": ["Responsible for managing the data pipeline migration."],
        }]
        saved = _run_tailor_application(db, application, experience, [])

        # C10 never rewrites -- the bullet is saved exactly as tailored.
        assert saved["experience"][0]["bullets"][0] == "Responsible for managing the data pipeline migration."

        application_row = db.query(JobApplication).filter(JobApplication.id == application.id).first()
        assert application_row.attention_reason is None


def _config_with_test_project_slugs():
    """The real config, deep-copied, with ONLY projects_by_variant's
    data_engineering slugs swapped for this test's short fixture names
    -- everything else (metrics/skills/experience_classification, all
    of which the rest of tailor_application()'s pipeline still reads
    from this same config) stays real and valid. Deliberately not a
    from-scratch minimal dict: the real config/resume_rules.yaml's own
    project slugs are derived from the live profile's actual project
    titles and legitimately change whenever those do (see that file's
    own comment), so this test shouldn't be coupled to whatever it
    currently says."""
    import copy

    from app.services import resume_rules

    config = copy.deepcopy(resume_rules.get_config())
    config["projects_by_variant"]["variants"]["data_engineering"] = [
        "career_pilot", "talentvenue_eventintel", "ai_model_observability",
    ]
    return config


class TestProjectSelectionWiredByVariant:
    def test_only_configured_projects_for_the_variant_are_passed_to_tailoring(self, db):
        profile = {
            "name": "Test", "summary": "d", "skills": {}, "experience": [],
            "projects": [
                {"name": "Career Pilot", "bullets": ["real bullet"]},
                {"name": "Some Other Project", "bullets": ["real bullet"]},
                {"name": "TalentVenue EventIntel", "bullets": ["real bullet"]},
                {"name": "AI Model Observability", "bullets": ["real bullet"]},
            ],
            "education": [], "certifications": [],
        }
        application = _application(db, profile, variant_name="Data Engineering")

        with patch("app.services.resume_rules.get_config", return_value=_config_with_test_project_slugs()):
            with patch("app.services.tailoring_service.run_multi_pass_tailoring") as mock_tailor:
                mock_tailor.return_value = ([], [], 90, [], [])
                with (
                    patch("app.services.tailoring_service._tailor_summary_skills", return_value={"summary": "d", "skills": {}}),
                    patch("app.services.tailoring_service.generate_cover_letter", return_value="cl"),
                    patch("app.services.tailoring_service.score_cover_letter", return_value=80),
                ):
                    tailoring_service.tailor_application(db, application.id)

        passed_projects = mock_tailor.call_args[0][1]
        names = {p["name"] for p in passed_projects}
        # "Some Other Project" isn't in the data_engineering variant's
        # configured list -- it must never reach the tailoring LLM call.
        assert names == {"Career Pilot", "TalentVenue EventIntel", "AI Model Observability"}
