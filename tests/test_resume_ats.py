"""resume_ats: rules from ATS and recruiter research -- exact posting wording, the posting's title in the headline,
keywords shown in bullets, one page for new-grad postings, and no GPA off the 4.0 scale."""
import io
import json
import re
from datetime import date

import pdfplumber

from app.services import document_render_service, page_fit_service, resume_ats

JD = (
    "New College Grad - Data Scientist. Currently pursuing or recently completed a Bachelor's degree. Experience using "
    "Python, SQL and generative AI, including large language models (LLMs). Knowledge of statistical modeling, "
    "machine learning, data mining and data visualization. Cloud platforms such as Snowflake, Google Cloud or AWS."
)
PROFILE = json.dumps({
    "skills": {"ai": ["LLMs (OpenAI API)", "Prompt Engineering"], "data": ["SQL", "GCP (BigQuery)", "Snowflake", "AWS"],
               "ml": ["Python", "Regression", "Hypothesis Testing", "Tableau"]},
    "experience": [{"bullets": ["Built machine learning models with Python and SQL."]}],
})


class TestCorePostingTitle:
    def test_strips_new_grad_prefix_team_and_level(self):
        assert resume_ats.core_posting_title("New College Grad - Data Scientist, AI for Business Functions") == "Data Scientist"
        assert resume_ats.core_posting_title("Machine Learning Engineer, Level 4") == "Machine Learning Engineer"
        assert resume_ats.core_posting_title("Senior Data Engineer II") == "Data Engineer"
        assert resume_ats.core_posting_title("Data Engineer (Remote)") == "Data Engineer"

    def test_headline_check(self):
        assert resume_ats.headline_has_posting_title("Data Scientist | Predictive Analytics", "New Grad - Data Scientist")
        assert not resume_ats.headline_has_posting_title("Data Engineer & ML Engineer", "Data Scientist")

    def test_headline_keeps_profile_title_on_a_role_mismatch(self):
        assert resume_ats.headline_for_posting("Data Engineer", "Registered Nurse", "mismatch") == "Data Engineer"
        assert resume_ats.headline_for_posting("Data Engineer", "Senior Data Scientist, Ads", "match") == "Data Scientist"
        assert resume_ats.headline_for_posting("Data Engineer", "Data Scientist", "unknown") == "Data Scientist"


class TestGpa:
    def test_drops_a_gpa_off_the_four_point_scale(self):
        assert resume_ats.us_scale_gpa_only("Aug 2017 – Jun 2021 · GPA: 7.8/10") == "Aug 2017 – Jun 2021"
        assert resume_ats.us_scale_gpa_only("GPA: 82/100") is None

    def test_keeps_a_four_point_gpa(self):
        assert resume_ats.us_scale_gpa_only("Aug 2022 – May 2024 · GPA: 4.0/4.0") == "Aug 2022 – May 2024 · GPA: 4.0/4.0"
        assert resume_ats.us_scale_gpa_only("May 2020 | GPA 3.6/4") == "May 2020 | GPA 3.6/4"

    def test_rendered_pdf_has_no_ten_point_gpa(self):
        doc = {"name": "T", "contact": {}, "summary": "S.", "skills": {}, "experience": [], "projects": [],
               "education": [{"degree": "B.Tech", "school": "Inst", "date": "2017 – 2021 · GPA: 7.8/10"},
                             {"degree": "M.S.", "school": "ASU", "date": "2022 – 2024 · GPA: 4.0/4.0"}]}
        text = pdfplumber.open(io.BytesIO(document_render_service.render_resume_pdf(doc))).pages[0].extract_text()
        assert "7.8/10" not in text and "4.0/4.0" in text and "2017" in text


class TestPageLimit:
    def test_new_grad_postings_get_one_page(self):
        cfg = {"page_fit": {"max_pages": 2, "new_grad_max_pages": 1}}
        assert resume_ats.page_limit_for_posting("New College Grad - Data Scientist", "", cfg) == 1
        assert resume_ats.page_limit_for_posting("Data Analyst", "0-2 years of relevant experience", cfg) == 1
        assert resume_ats.page_limit_for_posting("Data Engineer", "3+ years building pipelines", cfg) is None

    def test_no_limit_without_the_setting(self):
        assert resume_ats.page_limit_for_posting("New Grad Data Scientist", "", {"page_fit": {"max_pages": 2}}) is None


class TestExactWording:
    def test_report_splits_on_resume_reword_and_ask(self):
        resume = "Data Scientist. Python, SQL, LLMs, GCP (BigQuery), Snowflake, machine learning, Tableau dashboards."
        rep = resume_ats.exact_phrase_report(resume, JD, PROFILE)
        reword = {w["term"]: w for w in rep["reword"]}
        assert {"Python", "SQL", "Snowflake", "machine learning"} <= set(rep["on_resume"])
        assert reword["large language models"]["resume_form"] == "LLMs"
        assert reword["Google Cloud"]["resume_form"] == "GCP"
        assert reword["statistical modeling"]["backed_by"] == "evidence"  # regression / hypothesis testing in the profile
        assert "data visualization" in reword  # backed by Tableau
        assert set(rep["not_in_profile"]) == {"data mining", "generative AI"}  # nothing in this profile backs them: ask, never add

    def test_short_forms_match_case_sensitively(self):
        assert not resume_ats.has_term("said the team", "AI")
        assert resume_ats.has_term("Built AI agents", "AI")

    def test_skills_get_the_postings_form_without_new_skills(self):
        skills = {"ai": ["LLMs (OpenAI API, GPT-4o mini)", "AI Agents & Tool Calling"], "cloud": ["GCP (BigQuery)", "AWS"]}
        out, changes = resume_ats.add_posting_forms_to_skills(skills, JD + " Artificial Intelligence")
        assert out["ai"][0] == "Large Language Models (LLMs, OpenAI API, GPT-4o mini)"
        assert out["ai"][1] == "AI Agents & Tool Calling"  # only a skill that IS the term is renamed
        assert out["cloud"] == ["Google Cloud (GCP, BigQuery)", "AWS"]
        assert len(changes) == 2
        assert sum(map(len, out.values())) == sum(map(len, skills.values()))

    def test_long_form_gets_the_short_form_when_the_posting_uses_it(self):
        out, _ = resume_ats.add_posting_forms_to_skills({"x": ["Natural Language Processing"]}, "NLP experience")
        assert out["x"] == ["Natural Language Processing (NLP)"]


class TestKeywordsOnlyInSkills:
    def test_flags_terms_never_shown_in_work(self):
        doc = {"title": "Data Scientist", "summary": "Builds models.", "skills": {"a": ["Python", "Tableau", "LLMs"]},
               "experience": [{"bullets": ["Built Python pipelines."]}],
               "projects": [{"bullets": ["Fine-tuned large language models."]}]}
        assert resume_ats.keywords_only_in_skills(doc, ["Python", "Tableau", "LLMs"]) == ["Tableau"]


_BULLET = "Did a substantial thing involving real detail, context, outcomes and 25% metrics, written long enough to use space."


def _long_resume():
    year = date.today().year
    return {
        "name": "Test Candidate", "title": "Data Scientist", "contact": {"email": "t@example.com"},
        "summary": "A concise summary. " * 3, "skills": {"Languages": ["Python", "SQL"]},
        "experience": [{"role": f"Role {i}", "company": f"Company {i}", "date": f"{year - 1} - Present", "bullets": [_BULLET] * 5}
                       for i in range(5)],
        "projects": [{"name": f"Project {i}", "bullets": [_BULLET] * 4} for i in range(3)],
        "education": [{"degree": "M.S.", "school": "State University", "date": "2024"}],
        "languages": [{"language": "English", "proficiency": "Native"}],
    }


class TestOnePage:
    def test_plan_keeps_experience_ahead_of_projects(self):
        out = resume_ats.trim_for_one_page(_long_resume())
        assert [len(e["bullets"]) for e in out["experience"]] == [2, 2, 2, 1, 1]
        assert [len(p["bullets"]) for p in out["projects"]] == [2, 1]
        assert "languages" not in out

    def test_page_limit_renders_one_real_page(self, db):
        doc = dict(_long_resume(), page_limit=1)
        result = page_fit_service.render_resume_pdf_with_fit(db, doc)
        assert len(pdfplumber.open(io.BytesIO(result["pdf_bytes"])).pages) == 1
        assert "drop_weakest_bullet_per_role" not in result["reductions_applied"]

    def test_without_a_limit_the_default_still_applies(self, db):
        result = page_fit_service.render_resume_pdf_with_fit(db, _long_resume())
        assert len(pdfplumber.open(io.BytesIO(result["pdf_bytes"])).pages) == 2


class TestShowSkillsInBullets:
    PROFILE = {"experience": [{"company": "Acme", "role": "Data Engineer", "date": "Jan 2025 - Present",
                               "bullets": ["Cut costs 20% with Python.", "Built Kafka streams for 3 teams."]}],
               "projects": [{"name": "Lake", "bullets": ["Built a dbt model.", "Ran Terraform for the warehouse."]}]}

    def _doc(self):
        return {"title": "Data Engineer", "summary": "S.", "skills": {"a": ["Kafka", "Terraform", "Looker"]},
                "experience": [{"company": "Acme", "role": "Data Engineer", "date": "Jan 2025 - Present",
                                "bullets": ["Cut costs 20% with Python."]}],
                "projects": [{"name": "Lake", "bullets": ["Built a dbt model."]}]}

    def test_adds_the_profiles_own_bullet_that_shows_the_term(self):
        doc, notes = resume_ats.add_bullets_showing_terms(self._doc(), self.PROFILE, ["Kafka", "Terraform", "Looker"])
        assert doc["experience"][0]["bullets"][-1] == "Built Kafka streams for 3 teams."
        assert doc["projects"][0]["bullets"][-1] == "Ran Terraform for the warehouse."
        assert len(notes) == 2  # Looker has no profile bullet: nothing is written for it
        assert resume_ats.keywords_only_in_skills(doc, ["Kafka", "Terraform", "Looker"]) == ["Looker"]

    def test_never_adds_to_a_role_printed_as_an_earlier_line(self):
        old = {"company": "Old Co", "role": "Data Analyst", "date": "Aug 2015 - Mar 2016", "bullets": []}
        profile = {"experience": [dict(old, bullets=["Built Tableau dashboards for sales."])], "projects": []}
        doc = {"skills": {"a": ["Tableau"]}, "experience": [old], "projects": []}
        out, notes = resume_ats.add_bullets_showing_terms(doc, profile, ["Tableau"])
        assert notes == [] and out["experience"][0]["bullets"] == []


class TestNumbersAndStockPhrases:
    def test_quantified_share(self):
        doc = {"experience": [{"bullets": ["Cut costs 20%.", "Wrote docs."]}], "projects": [{"bullets": ["Served 5,000 users."]}]}
        assert resume_ats.quantified_share(doc) == (2, 3)

    def test_stock_phrases(self):
        assert resume_ats.stock_summary_phrases("Engineer. Ready to bring results-driven work.") == ["ready to bring", "results-driven"]
        assert resume_ats.stock_summary_phrases("Focused on data engineering roles.") == []


def test_projects_heading_is_plain():
    doc = {"name": "T", "contact": {}, "summary": "S.", "skills": {}, "experience": [], "education": [],
           "projects": [{"name": "P", "bullets": ["Did a thing."]}]}
    text = pdfplumber.open(io.BytesIO(document_render_service.render_resume_pdf(doc))).pages[0].extract_text()
    assert "PROJECTS" in text and "KEY PROJECTS" not in text


class TestPostingWordsForRealWork:
    """His rule (2026-11-15): the profile says what was done; the posting decides how it is said. Rewording is not faking."""

    def test_singular_and_plural_both_match(self):
        assert resume_ats.has_term("Built an AI agent with tool calling", "AI agents")
        assert resume_ats.has_term("a published Tableau dashboard", "dashboards")
        assert not resume_ats.has_term("Built an LLM app", "LLMs")  # short forms stay exact

    def test_backed_terms_cover_work_under_other_words(self):
        jd = "We need data modeling, ELT with dbt, data governance, model monitoring and Kafka."
        profile = "Modernized Star Schema dimensional models; dbt project; PostgreSQL row-level security; KS drift checks."
        terms = {t["term"] for t in resume_ats.backed_posting_terms(jd, profile)}
        assert {"data modeling", "ELT", "data governance", "model monitoring", "dbt"} <= terms
        assert "Kafka" not in terms  # no work shows it: never reworded in

    def test_guard_accepts_the_postings_word_for_real_work(self):
        from app.services.tailoring_service import _find_unsupported_keywords
        profile = {"experience": [{"bullets": ["Modernized Star Schema dimensional models in Snowflake."]}]}
        assert _find_unsupported_keywords(profile, ["data modeling", "Kafka"]) == ["Kafka"]
