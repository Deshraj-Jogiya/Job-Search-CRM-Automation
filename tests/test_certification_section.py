"""Certifications & Training is a rule for every resume, weighed against the job and the resume's fit: strong experience and projects come first,
credentials are only mentioned unless their pointers would add what the experience and projects lack, and the choice is wired into real tailoring."""

import copy
import json
from unittest.mock import patch

from app.models import Company, JobApplication, JobPosting, ProfileVariant, ProfileVersion, TailoredDocument
from app.services import resume_rules, tailoring_service
from app.services.company_utils import normalize_company_name

HR_CERT = {"name": "Microsoft HR Manager Professional Certificate", "issuer": "Microsoft on Coursera", "status": "Completed",
           "bullets": ["Configured HRIS modules with eligibility rules and approval workflows.", "Built Power BI dashboards of turnover and time-to-hire.",
                       "Applied 5-Whys root-cause analysis to HR process problems.", "Tracked policy acknowledgments."]}
DATA_CERT = {"name": "IBM Data Analyst Professional Certificate", "issuer": "IBM on Coursera", "status": "Completed", "date": "Sep 2025",
             "bullets": ["Cleaned data with Python and Pandas.", "Built SQL queries and Cognos dashboards."]}
IN_PROGRESS = {"name": "People Analytics with AI Specialization", "issuer": "Coursera", "status": "In progress", "bullets": ["Covers HR metrics and workforce dashboards."]}
HIGHLIGHT = "Guinness World Record — AI Training Hackathon (Kanz) — Jul 2026"
TABLEAU = {"name": "Tableau Business Intelligence Analyst Professional Certificate", "issuer": "Tableau Learning Partner on Coursera", "status": "Completed",
           "date": "Feb 2026", "bullets": ["Built interactive Tableau dashboards."]}
CERTS = [DATA_CERT, HIGHLIGHT, HR_CERT, IN_PROGRESS, TABLEAU]

HR_JD = "HRIS analyst: configure HRIS applications, HR technology, eligibility rules, approval workflows, HR processes, talent management systems."
DATA_JD = "Data engineer: build Python and SQL data pipelines on Snowflake and Airflow, dashboards, data quality testing."

LACKING = {"experience": [{"role": "Data Engineer", "company": "Acme", "bullets": ["Built Python SQL pipelines on Snowflake with Airflow."]}], "projects": []}
STRONG = {"experience": [{"role": "Data Engineer", "company": "Acme", "bullets": ["Built Python and SQL data pipelines on Snowflake and Airflow with data quality testing and dashboards."]}],
          "projects": [{"name": "Pipeline", "technologies": ["Python", "SQL", "Airflow"], "bullets": ["Built dashboards and data quality tests."]}]}


def test_legacy_strings_keep_their_date_and_have_no_issuer_or_pointers():
    e = resume_rules.normalize_certification(HIGHLIGHT)
    assert e["name"] == "Guinness World Record — AI Training Hackathon (Kanz)" and e["date"] == "Jul 2026" and e["issuer"] is None and e["bullets"] == []


def test_structured_entry_defaults_and_invalid_status_is_not_trusted():
    e = resume_rules.normalize_certification({"name": "X", "status": "Done!"})
    assert e["status"] == "Completed" and e["bullets"] == []
    assert resume_rules.normalize_certification({"name": "X", "status": "In progress"})["status"] == "In progress"


def test_display_line_shows_issuer_and_status_for_structured_entries_and_passes_strings_through():
    assert resume_rules.certification_display(DATA_CERT) == "IBM Data Analyst Professional Certificate | IBM on Coursera | Completed, Sep 2025"
    assert resume_rules.certification_display(IN_PROGRESS) == "People Analytics with AI Specialization | Coursera | In progress"
    assert resume_rules.certification_display("Plain string cert") == "Plain string cert"


def test_pointers_when_the_job_needs_what_experience_and_projects_lack():
    plan = resume_rules.plan_certification_section(CERTS, HR_JD, LACKING)
    assert plan["mode"] == "pointers" and plan["total_gain"] >= 0.10 and plan["coverage"] < 0.60
    assert plan["entries"][0]["name"].startswith("Microsoft HR Manager")
    assert plan["entries"][0]["bullets"][0].startswith("Configured HRIS modules")  # the pointer that adds the job's missing terms comes first
    assert all(len(e["bullets"]) <= 3 for e in plan["entries"]) and len(plan["entries"]) <= 4
    assert set(plan["entries"][0]["bullets"]) <= set(HR_CERT["bullets"])  # nothing invented


def test_mention_only_when_experience_and_projects_already_carry_the_job():
    plan = resume_rules.plan_certification_section(CERTS, DATA_JD, STRONG)
    assert plan["mode"] == "mention" and plan["entries"] == []
    assert "without pointers" in plan["reason"] or "already carry" in plan["reason"]
    assert not any(line.startswith("Guinness") for line in plan["lines"])  # a configured highlight still has to relate to the job
    assert plan["lines"][0].startswith("IBM Data Analyst")
    ai = resume_rules.plan_certification_section(CERTS, "AI data engineer: build Python and SQL data pipelines on Snowflake and Airflow, AI dashboards, data quality testing.", STRONG)
    assert ai["lines"][0].startswith("Guinness World Record")  # relevant highlights lead, in configured order
    assert not any("Microsoft HR Manager" in line for line in plan["lines"])  # an HR credential is not listed on a data-engineering resume


def test_credentials_that_add_nothing_to_the_job_never_get_pointers_even_when_the_resume_is_weak():
    plan = resume_rules.plan_certification_section([HR_CERT], "Kubernetes platform engineer: Go, Terraform, Helm, service mesh.", LACKING)
    assert plan["mode"] != "pointers" and plan["entries"] == []


def test_pointers_need_pointers_to_give():
    plan = resume_rules.plan_certification_section(["Human resources management certificate - Jan 2025"], HR_JD, LACKING)
    assert plan["entries"] == []


def test_in_progress_status_is_shown_and_finished_credentials_come_first():
    plan = resume_rules.plan_certification_section(CERTS, HR_JD, LACKING)
    statuses = [e["status"].split(",")[0] for e in plan["entries"]]
    assert statuses == sorted(statuses, key=lambda s: s != "Completed")
    assert any(e["status"] == "In progress" for e in plan["entries"]) or len(plan["entries"]) < 3
    dated = resume_rules.plan_certification_section([DATA_CERT], "Data analyst: Python SQL dashboards cleaning Pandas Cognos", {"experience": [], "projects": []})
    assert dated["entries"][0]["status"] == "Completed, Sep 2025"


def test_section_can_be_switched_off_and_empty_profile_gives_nothing():
    cfg = copy.deepcopy(resume_rules.get_config())
    cfg["certifications"]["section"]["enabled"] = False
    assert resume_rules.plan_certification_section(CERTS, HR_JD, LACKING, cfg)["mode"] == "none"
    assert resume_rules.plan_certification_section([], HR_JD, LACKING)["mode"] == "none"


def test_company_name_words_do_not_count_as_job_terms():
    terms = resume_rules.certification_core_terms("Acme Robotics is hiring an HRIS analyst for Acme Robotics HRIS systems", "Acme Robotics")
    assert "acme" not in terms and "robotics" not in terms and "hris" in terms


def test_legacy_list_curation_accepts_structured_entries():
    out = resume_rules.select_certifications_for_resume([DATA_CERT, HIGHLIGHT, HR_CERT])
    assert HIGHLIGHT in out[:1]


def _application(db, profile_content, jd, company="Acme Corp"):
    variant = ProfileVariant(name="Data Engineering", is_default=True)
    db.add(variant)
    db.commit()
    db.refresh(variant)
    db.add(ProfileVersion(variant_id=variant.id, content_json=json.dumps(profile_content), source="manual", is_active=True))
    db.commit()
    co = Company(name=company, normalized_name=normalize_company_name(company))
    db.add(co)
    db.commit()
    db.refresh(co)
    posting = JobPosting(company_id=co.id, company_name_raw=company, job_title="Analyst", job_description=jd, source="greenhouse")
    db.add(posting)
    db.commit()
    db.refresh(posting)
    app = JobApplication(posting_id=posting.id)
    db.add(app)
    db.commit()
    db.refresh(app)
    return app


def _tailor(db, app, experience, projects):
    with (
        patch("app.services.tailoring_service.run_multi_pass_tailoring", return_value=(experience, projects, 90, [], [])),
        patch("app.services.tailoring_service._tailor_summary_skills", return_value={"summary": "s", "skills": {}}),
        patch("app.services.tailoring_service.generate_cover_letter", return_value="A cover letter."),
        patch("app.services.tailoring_service.score_cover_letter", return_value=80),
    ):
        tailoring_service.tailor_application(db, app.id)
    return json.loads(db.query(TailoredDocument).filter_by(application_id=app.id, document_type="resume").first().content)


def _profile():
    return {"name": "Test", "summary": "d", "skills": {}, "experience": [], "projects": [], "education": [], "certifications": CERTS}


def test_tailoring_pipeline_adds_pointers_after_experience_when_the_resume_lacks_the_job(db):
    app = _application(db, _profile(), HR_JD)
    doc = _tailor(db, app, [], [])
    assert doc["certification_plan"]["mode"] == "pointers"
    assert doc["certification_entries"][0]["name"].startswith("Microsoft HR Manager") and doc["certification_entries_title"] == "Certifications & Training"
    assert doc["certifications"] == []


def test_tailoring_pipeline_keeps_credentials_to_a_compact_list_when_experience_and_projects_are_strong(db):
    app = _application(db, _profile(), DATA_JD)
    exp = [{"role": "Data Engineer", "company": "Acme", "bullets": ["Built Python and SQL data pipelines on Snowflake and Airflow with data quality testing and dashboards."]}]
    pro = [{"name": "Pipeline", "technologies": ["Python", "SQL", "Airflow"], "bullets": ["Built dashboards and data quality tests."]}]
    doc = _tailor(db, app, exp, pro)
    assert doc["certification_plan"]["mode"] == "mention" and "certification_entries" not in doc
    assert doc["certifications"] and all(isinstance(x, str) for x in doc["certifications"]) and len(doc["certifications"]) <= 3


def test_tailoring_pipeline_lists_no_credentials_when_none_relates_to_the_job(db):
    profile = dict(_profile(), certifications=[HR_CERT, IN_PROGRESS])
    app = _application(db, profile, "Kubernetes platform engineer: Go, Terraform, Helm, service mesh, distributed systems.")
    exp = [{"role": "Platform Engineer", "company": "Acme", "bullets": ["Ran Kubernetes clusters with Terraform and Helm."]}]
    doc = _tailor(db, app, exp, [])
    assert doc["certifications"] == [] and "certification_entries" not in doc  # no fallback to unrelated credentials


def test_page_fit_trims_certification_pointers_but_keeps_one_per_entry():
    from app.services import page_fit_service as pf
    content = {"certification_entries": [{"name": "A", "bullets": ["1", "2", "3"]}, {"name": "B", "bullets": ["x"]}]}
    cfg = resume_rules.get_config()
    once = pf._apply_content_reduction(content, "trim_certification_pointers", cfg)
    assert once["certification_entries"][0]["bullets"] == ["1", "2"] and once["certification_entries"][1]["bullets"] == ["x"]
    twice = pf._apply_content_reduction(once, "trim_certification_pointers", cfg)
    thrice = pf._apply_content_reduction(twice, "trim_certification_pointers", cfg)
    assert thrice["certification_entries"][0]["bullets"] == ["1"]
    assert pf._is_reduction_exhausted("trim_certification_pointers", thrice, None, cfg)
    assert content["certification_entries"][0]["bullets"] == ["1", "2", "3"]  # input never mutated


def test_hide_work_authorization_flag_removes_the_visa_line_in_pdf_and_docx(monkeypatch):
    monkeypatch.setattr(resume_rules, "work_authorization_line", lambda config=None: "Authorized to work in the U.S. now on F-1 STEM OPT; future H-1B sponsorship needed.")
    import io
    import pdfplumber
    from app.services.docx_generator import build_resume_docx
    from app.services.document_render_service import render_resume_pdf
    base = {"name": "T P", "contact": {}, "summary": "s", "experience": [], "projects": []}
    assert "Authorized to work" in "\n".join(p.text for p in build_resume_docx(base).paragraphs)
    hidden = dict(base, hide_work_authorization=True)
    assert "Authorized to work" not in "\n".join(p.text for p in build_resume_docx(hidden).paragraphs)
    pdf_text = "\n".join(pg.extract_text() or "" for pg in pdfplumber.open(io.BytesIO(render_resume_pdf(json.dumps(hidden)))).pages)
    assert "Authorized to work" not in pdf_text
    pdf_shown = "\n".join(pg.extract_text() or "" for pg in pdfplumber.open(io.BytesIO(render_resume_pdf(json.dumps(base)))).pages)
    assert "Authorized to work" in pdf_shown


def test_plain_certification_list_is_not_printed_when_the_section_is_present():
    from app.services.docx_generator import build_resume_docx
    resume = {"name": "T", "contact": {}, "experience": [], "projects": [], "certifications": ["Old Plain Cert"],
              "certification_entries": [{"name": "Cert A", "issuer": "I", "status": "Completed", "bullets": ["x"]}]}
    text = "\n".join(p.text for p in build_resume_docx(resume).paragraphs)
    assert "Cert A" in text and "Old Plain Cert" not in text
    assert "Old Plain Cert" in "\n".join(p.text for p in build_resume_docx(dict(resume, certification_entries=[])).paragraphs)


def test_mention_lines_are_compact_and_keep_the_status_honest():
    assert resume_rules.certification_mention_line(TABLEAU) == "Tableau Business Intelligence Analyst Professional Certificate (Tableau Learning Partner on Coursera), Feb 2026"
    assert resume_rules.certification_mention_line(IN_PROGRESS) == "People Analytics with AI Specialization (Coursera), in progress"
    assert resume_rules.certification_mention_line(HIGHLIGHT) == "Guinness World Record \u2014 AI Training Hackathon (Kanz), Jul 2026"
    assert resume_rules.certification_mention_line({"name": "X", "issuer": "Y | 5 courses, 40 hours", "status": "Completed"}) == "X (Y)"


def test_additional_certificates_name_the_credentials_that_did_not_fit_finished_ones_first():
    plan = resume_rules.plan_certification_section(CERTS, HR_JD, LACKING)
    shown = {e["name"] for e in plan["entries"]}
    assert plan["additional"] and not any(n.split(" (in progress)")[0] in shown for n in plan["additional"])
    done = [n for n in plan["additional"] if "(in progress)" not in n]
    assert plan["additional"][: len(done)] == done  # finished credentials before in-progress ones
    mention = resume_rules.plan_certification_section(CERTS, DATA_JD, STRONG)
    assert mention["mode"] == "mention" and not any("Microsoft HR Manager" in n for n in mention["additional"])  # unrelated to a data job: left off
    assert not any(n.startswith("Guinness") for n in mention["additional"])  # already in the compact list


def test_additional_certificates_are_capped_by_config():
    cfg = copy.deepcopy(resume_rules.get_config())
    cfg["certifications"]["section"]["additional_max_entries"] = 1
    assert len(resume_rules.plan_certification_section(CERTS, HR_JD, LACKING, cfg)["additional"]) == 1


def test_additional_line_is_printed_after_the_section_in_pdf_and_docx_and_in_the_compact_list():
    import io
    import pdfplumber
    from app.services.docx_generator import build_resume_docx
    from app.services.document_render_service import render_resume_pdf
    pointers = {"name": "T", "contact": {}, "experience": [], "projects": [], "certification_entries": [{"name": "Cert A", "issuer": "I", "status": "Completed", "bullets": ["x"]}],
                "additional_certifications": ["Cert B", "Cert C (in progress)"]}
    compact = {"name": "T", "contact": {}, "experience": [], "projects": [], "certifications": ["Cert A (I), 2026"], "additional_certifications": ["Cert B"]}
    for resume, line in ((pointers, "Additional certificates: Cert B; Cert C (in progress)"), (compact, "Additional certificates: Cert B")):
        assert line in "\n".join(p.text for p in build_resume_docx(resume).paragraphs)
        pdf = "\n".join(pg.extract_text() or "" for pg in pdfplumber.open(io.BytesIO(render_resume_pdf(json.dumps(resume)))).pages)
        assert line in pdf and pdf.index("Cert A") < pdf.index(line)


def test_page_fit_drops_the_additional_line_before_anything_else(db):
    from app.services import page_fit_service as pf
    bullets = [f"Built and shipped a distinct, measurable feature number {i} with tests and documentation across the platform and its services." for i in range(70)]
    resume = {"name": "T", "contact": {}, "summary": "s", "experience": [{"role": "Engineer", "company": "Acme", "start_date": "2022-01", "end_date": "2024-01", "bullets": bullets}],
              "projects": [], "additional_certifications": ["Cert B"]}
    out = pf.render_resume_pdf_with_fit(db, resume)
    assert out["reductions_applied"][0] == "drop_additional_certificates" and len(out["reductions_applied"]) > 1


def test_additional_line_appears_once_and_only_after_the_certificate_section():
    from app.services.docx_generator import build_resume_docx
    resume = {"name": "T", "contact": {}, "experience": [{"role": "Engineer", "company": "Acme", "start_date": "2022-01", "end_date": "2024-01", "bullets": ["Did a thing."]}],
              "projects": [{"name": "P", "bullets": ["Built it."]}], "certifications": ["Cert A"], "additional_certifications": ["Cert B"]}
    paras = [p.text for p in build_resume_docx(resume).paragraphs]
    assert paras.count("Additional certificates: Cert B") == 1
    assert paras.index("Additional certificates: Cert B") > paras.index("Certifications") > paras.index("Selected Projects")


def test_additional_line_skips_credentials_unrelated_to_the_job():
    entries = [resume_rules.normalize_certification(c) for c in [
        {"name": "IBM Data Analyst Professional Certificate", "issuer": "IBM on Coursera", "status": "Completed", "bullets": []},
        {"name": "HRCI Human Resource Associate Professional Certificate", "issuer": "HRCI on Coursera", "status": "Completed", "bullets": []},
        {"name": "Greenhouse Training Resources", "issuer": "Greenhouse Support", "status": "In progress", "bullets": []},
    ]]
    core = resume_rules.certification_core_terms("Data analyst role: SQL, Power BI dashboards, ETL, data models, predictive modeling and data quality.", "", 15)
    names = resume_rules.additional_certification_names(entries, set(), core, 6)
    assert names == ["IBM Data Analyst Professional Certificate"]


def test_component_courses_stay_in_the_profile_but_are_never_listed_separately():
    course = {"name": "Databases and SQL for Data Science with Python", "issuer": "IBM on Coursera", "status": "Completed", "component_of": "IBM Data Analyst Professional Certificate"}
    plan = resume_rules.plan_certification_section([DATA_CERT, course], DATA_JD, STRONG)
    shown = plan["lines"] + plan["additional"] + [e["name"] for e in plan["entries"]]
    assert not any("Databases and SQL" in s for s in shown)


def test_a_mention_line_does_not_repeat_an_issuer_already_in_the_name():
    # Live 2026-10-25: "Guinness World Record - AI Training Hackathon (Kanz) (Kanz), Jul 2026"
    entry = {"name": "AI Training Hackathon (Kanz)", "issuer": "Kanz", "status": "Completed", "date": "Jul 2026", "bullets": []}
    assert resume_rules.certification_mention_line(entry) == "AI Training Hackathon (Kanz), Jul 2026"
    plain = {"name": "Generative AI Mastermind", "issuer": "Outskill", "status": "Completed", "date": "Oct 2025", "bullets": []}
    assert resume_rules.certification_mention_line(plain) == "Generative AI Mastermind (Outskill), Oct 2025"
