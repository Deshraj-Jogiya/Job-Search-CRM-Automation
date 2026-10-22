import json
from unittest.mock import patch

import pytest

from app import models
from app.services import fit_service, matching_service, outreach_service, tailoring_service
from tests.conftest import make_company, make_posting, make_variant

DATA_ENGINEER = {
    "name": "Dana Lee", "title": "Data Engineer", "summary": "Data engineer.",
    "skills": {"languages": ["Python", "SQL"], "data": ["Airflow", "Spark", "dbt", "Kafka"], "cloud": ["AWS (S3, Redshift)"]},
    "experience": [{"role": "Data Engineer", "company": "Acme", "date": "Jan 2021 - Present",
                    "bullets": ["Built Airflow pipelines loading 2TB a day into Redshift.", "Wrote dbt models for finance.",
                                "Ran a Kafka to Spark streaming job."]}],
    "projects": [{"name": "Lakehouse", "bullets": ["Delta tables on S3."], "technologies": ["Spark", "Python"]}],
}
SOFTWARE_ENGINEER = {
    "name": "Sam Park", "title": "Software Engineer", "summary": "Backend engineer.",
    "skills": {"languages": ["Java", "Python", "JavaScript", "SQL"], "tools": ["React", "Docker", "Kubernetes", "AWS"]},
    "experience": [{"role": "Software Engineer", "company": "Webco", "date": "Jan 2020 - Present",
                    "bullets": ["Built REST APIs in Java and Spring.", "Deployed services on Kubernetes."]}],
    "projects": [],
}
JUNIOR = dict(DATA_ENGINEER, experience=[dict(DATA_ENGINEER["experience"][0], date="Jan 2025 - Present")])

DATA_ANALYST_JD = """Data Analyst
Requirements
- 2+ years with SQL and Excel
- Build dashboards in Tableau or Power BI
- Statistics and A/B testing
Nice to have
- Python"""
NURSE_JD = "Registered Nurse\nRequirements\n- Active RN license\n- 3+ years of patient care\n- BLS certification"
STAFF_DE_JD = """Staff Data Engineer
Requirements
- 10+ years building data platforms
- Airflow, Spark, Kafka and dbt
- AWS and Redshift"""
DE_JD = """Data Engineer
Requirements
- 3+ years with Python and SQL
- Airflow and dbt in production
- Spark or Kafka
Nice to have
- Snowflake"""


class TestMismatchesScoreLow:
    """The fixed set of pairs that must never score well, whatever the tools overlap."""

    def test_software_engineer_resume_on_a_data_analyst_posting(self):
        fit = fit_service.rules_fit(SOFTWARE_ENGINEER, "Data Analyst", DATA_ANALYST_JD)
        assert fit["role"]["status"] == "mismatch"
        assert fit["score"] <= fit_service.ROLE_MISMATCH_CAP

    def test_data_engineer_on_a_nursing_role(self):
        fit = fit_service.rules_fit(DATA_ENGINEER, "Registered Nurse", NURSE_JD)
        assert fit["skills"]["requirements_found"] == 0
        assert fit["score"] < 30

    def test_junior_profile_on_a_staff_role(self):
        fit = fit_service.rules_fit(JUNIOR, "Staff Data Engineer", STAFF_DE_JD)
        assert fit["years"]["required_years"] == 10 and fit["years"]["profile_years"] < 3
        assert fit["score"] < fit_service.rules_fit(DATA_ENGINEER, "Staff Data Engineer", STAFF_DE_JD)["score"]

    def test_a_real_match_scores_high_with_evidence(self):
        fit = fit_service.rules_fit(DATA_ENGINEER, "Data Engineer", DE_JD)
        assert fit["role"]["status"] == "match" and fit["cap_reason"] is None
        assert fit["score"] >= 80
        where = {m["term"]: m["evidence"] for m in fit["skills"]["matched"]}
        assert where["Airflow"] == "Data Engineer at Acme"
        assert fit["skills"]["transferable"][0]["term"] == "Snowflake"  # Redshift counts as a comparable warehouse


class TestRoleTypes:
    def test_a_title_naming_two_role_types_matches_either(self):
        assert fit_service.role_check(DATA_ENGINEER, "Machine Learning Data Engineer")["status"] == "match"

    def test_unrecognised_titles_are_never_capped(self):
        role = fit_service.role_check(DATA_ENGINEER, "Member of Technical Staff")
        assert role["status"] == "unknown"
        assert fit_service.apply_role_cap(90, role) == (90, None)

    def test_a_role_type_held_before_is_not_a_mismatch(self):
        profile = dict(SOFTWARE_ENGINEER, experience=SOFTWARE_ENGINEER["experience"] + [{"role": "Data Analyst", "company": "Old", "bullets": []}])
        assert fit_service.role_check(profile, "Data Analyst")["status"] == "match"

    def test_search_keywords_count_as_targets(self):
        assert fit_service.role_check(SOFTWARE_ENGINEER, "Data Analyst", ["Data Analyst"])["status"] == "match"


def _scored_application(db, profile, title, jd):
    make_variant(db, content=profile)
    posting = make_posting(db, make_company(db), job_title=title, job_description=jd)
    app = models.JobApplication(posting_id=posting.id, status="Ingested")
    db.add(app)
    db.commit()
    return app


class TestScoringApplication:
    def test_an_inflated_ai_score_is_capped_before_anything_reads_it(self, db):
        app = _scored_application(db, SOFTWARE_ENGINEER, "Data Analyst", DATA_ANALYST_JD)
        ai = {"match_score": 88, "matching_skills": ["SQL"], "missing_keywords": [], "visa_sponsorship": "Unknown",
              "strengths": "Strong.", "gaps_analysis": "None."}
        with patch("app.services.matching_service.evaluate_match", return_value=ai):
            matching_service.score_application(db, app.id)
        db.refresh(app)
        analysis = json.loads(app.match_analysis_json)
        assert app.match_score == fit_service.ROLE_MISMATCH_CAP
        assert analysis["ai_score_before_cap"] == 88 and analysis["method"] == "ai"
        assert app.score_breakdown["fit"]["cap_reason"]
        assert app.score_breakdown["components"]["ai_profile_fit"]["raw"] == fit_service.ROLE_MISMATCH_CAP

    def test_without_an_ai_provider_the_rules_score_it(self, db, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        app = _scored_application(db, DATA_ENGINEER, "Data Engineer", DE_JD)
        with patch("app.services.matching_service.evaluate_match", side_effect=AssertionError("no AI call expected")):
            matching_service.score_application(db, app.id)
        db.refresh(app)
        analysis = json.loads(app.match_analysis_json)
        assert analysis["method"] == "rules"
        assert app.match_score == fit_service.rules_fit(DATA_ENGINEER, "Data Engineer", DE_JD)["score"]
        assert analysis["evidence"]["skills"]["matched"]

    def test_the_showcase_instance_never_calls_the_ai(self, db, monkeypatch):
        monkeypatch.setenv("APP_MODE", "showcase")
        app = _scored_application(db, DATA_ENGINEER, "Data Engineer", DE_JD)
        with patch("app.services.matching_service.evaluate_match", side_effect=AssertionError("no AI call expected")):
            matching_service.score_application(db, app.id)

    def test_backfill_caps_an_application_scored_before_the_cap(self, db):
        app = _scored_application(db, SOFTWARE_ENGINEER, "Data Analyst", DATA_ANALYST_JD)
        app.match_score = 88
        app.match_analysis_json = json.dumps({"match_score": 88, "strengths": "", "gaps_analysis": ""})
        db.commit()
        assert matching_service.refresh_fit_evidence(db, app) is True
        assert app.match_score == fit_service.ROLE_MISMATCH_CAP
        assert json.loads(app.match_analysis_json)["ai_score_before_cap"] == 88
        assert matching_service.refresh_fit_evidence(db, app) is False  # idempotent


class TestTailoring:
    def test_tailoring_does_not_lift_the_cap(self, db):
        app = _scored_application(db, SOFTWARE_ENGINEER, "Data Analyst", DATA_ANALYST_JD)
        with (
            patch("app.services.tailoring_service.run_multi_pass_tailoring",
                  return_value=(SOFTWARE_ENGINEER["experience"], [], 92, [], [])),
            patch("app.services.tailoring_service._tailor_summary_skills", return_value={"summary": "s", "skills": {}}),
            patch("app.services.tailoring_service.check_bullet_fabrication", return_value=[]),
            patch("app.services.tailoring_service.generate_cover_letter", return_value="A letter."),
            patch("app.services.tailoring_service.score_cover_letter", return_value=80),
        ):
            tailoring_service.tailor_application(db, app.id)
        db.refresh(app)
        assert app.match_score == fit_service.ROLE_MISMATCH_CAP

    def test_without_ai_it_reorders_and_never_rewrites(self, db, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY")
        profile = dict(DATA_ENGINEER, experience=[dict(DATA_ENGINEER["experience"][0], bullets=[
            "Mentored two junior engineers.", "Built Airflow pipelines loading 2TB a day into Redshift."])])
        app = _scored_application(db, profile, "Data Engineer", DE_JD)
        with patch("app.services.tailoring_service.run_multi_pass_tailoring", side_effect=AssertionError("no AI call expected")):
            tailoring_service.tailor_application(db, app.id)
        resume = json.loads(db.query(models.TailoredDocument).filter_by(application_id=app.id, document_type="resume").one().content)
        bullets = resume["experience"][0]["bullets"]
        assert bullets[0].startswith("Built Airflow")  # the bullet naming the posting's tools moved up
        assert sorted(bullets) == sorted(profile["experience"][0]["bullets"])  # same words, nothing added
        assert resume["summary"] == profile["summary"] and resume["tailoring_method"] == "rules"
        assert db.query(models.TailoredDocument).filter_by(application_id=app.id, document_type="cover_letter").count() == 0
        db.refresh(app)
        assert app.match_score == fit_service.rules_fit(profile, "Data Engineer", DE_JD)["score"]  # reordering does not change fit


def test_outreach_draft_without_ai_uses_only_facts_on_record(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    posting = models.JobPosting(job_title="Data Engineer", company_name_raw="Acme")
    note = outreach_service._draft_note_text(DATA_ENGINEER, posting, "Riley Chen", "linkedin_connection")
    assert note.startswith("Hi Riley,") and "Data Engineer at Acme" in note and len(note) < 300


@pytest.mark.parametrize("path,form", [
    ("/research-agent/ask", {"question": "How many applied?"}),
    ("/interview-prep/generate", {}),
])
def test_ai_only_routes_say_so_instead_of_failing(db, monkeypatch, path, form):
    from fastapi.testclient import TestClient
    from app.csrf import CSRF_COOKIE_NAME
    from app.main import app as web_app

    monkeypatch.setenv("APP_MODE", "showcase")
    client = TestClient(web_app)
    client.get("/how-scoring-works")
    token = client.cookies.get(CSRF_COOKIE_NAME)
    response = client.post(path, data={**form, "csrf_token": token}, follow_redirects=False)
    assert response.status_code == 303
    assert "error=" in response.headers["location"] and "AI%20provider" in response.headers["location"]
    assert db.query(models.ResearchAgentQuery).count() == 0


def test_how_scoring_works_page_shows_the_real_cap(db):
    from fastapi.testclient import TestClient
    from app.main import app as web_app

    page = TestClient(web_app).get("/how-scoring-works").text
    assert f"capped at <strong>{fit_service.ROLE_MISMATCH_CAP}</strong>" in page
    assert "Sponsorship history" in page
