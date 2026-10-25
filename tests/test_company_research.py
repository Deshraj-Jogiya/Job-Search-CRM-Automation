"""Company research for tailoring: the company's own site first, job boards left out, one cached brief per company, an
empty brief (posting-only tailoring) when search is unavailable, and the brief reaching project choice, the summary and
the cover letter. Tavily and the AI provider are patched everywhere."""

import json
from unittest.mock import MagicMock, patch

from app.models import Company
from app.services import company_research_service as research
from app.services import tailoring_service

from tests.test_tailor_application_prose_checks import _BASE_PROFILE, _application

RESULTS = [
    {"url": "https://www.linkedin.com/company/acme", "content": "Acme on LinkedIn."},
    {"url": "https://news.example.com/acme-raises", "content": "Acme raised a Series B to expand fleet analytics."},
    {"url": "https://www.acme.com/about", "content": "Acme builds route-planning software for 4,000 trucking fleets."},
]


def _patched(domain="acme.com", results=RESULTS, configured=True):
    search = MagicMock(return_value=results)
    return (
        patch.object(research, "is_tavily_configured", return_value=configured),
        patch.object(research, "_company_domain", return_value=domain),
        patch.object(research, "tavily_search", search),
        search,
    )


def test_own_site_first_job_boards_dropped_and_cached(db):
    company = Company(name="Acme", normalized_name="acme")
    db.add(company)
    db.commit()
    configured, find_domain, tavily, search = _patched()
    with configured, find_domain, tavily:
        brief = research.brief_for(db, company)
        again = research.brief_for(db, company)
    assert [f["url"] for f in brief["facts"]] == ["https://www.acme.com/about", "https://news.example.com/acme-raises"]
    assert brief["facts"][0]["own_site"] and not brief["facts"][1]["own_site"]
    assert search.call_count == 2  # the second call came from the cache
    assert again["facts"] == brief["facts"] and company.website_domain == "acme.com"
    assert "[company site] Acme builds route-planning" in research.prompt_block(brief)


def test_no_search_means_an_empty_brief_and_posting_only_tailoring(db):
    company = Company(name="Acme", normalized_name="acme")
    db.add(company)
    db.commit()
    configured, find_domain, tavily, search = _patched(configured=False)
    with configured, find_domain, tavily:
        assert research.brief_for(db, company)["facts"] == []
    assert search.call_count == 0 and research.prompt_block({"facts": []}) == ""


def test_an_empty_result_is_not_cached(db):
    company = Company(name="Acme", normalized_name="acme")
    db.add(company)
    db.commit()
    configured, find_domain, tavily, _ = _patched(results=[])
    with configured, find_domain, tavily:
        research.brief_for(db, company)
    assert company.research_brief is None and company.researched_at is None


def _prompt_of(fn, *args, json_mode=True, **kwargs):
    llm = MagicMock()
    llm.complete_json.return_value = '{"experience": [], "projects": [], "summary": "", "skills": {}}'
    llm.complete_text.return_value = "Body."
    with patch("app.services.tailoring_service.get_llm_provider", return_value=llm):
        fn(*args, **kwargs)
    call = llm.complete_json.call_args if json_mode else llm.complete_text.call_args
    return call.kwargs["prompt"]


def test_the_context_reaches_project_choice_summary_and_letter():
    context = research.prompt_block({"facts": [{"text": "Acme builds route-planning software.", "url": "u", "own_site": True}]})
    projects = _prompt_of(tailoring_service._tailor_experience_and_projects_pass, [], [], "JD", context)
    summary = _prompt_of(tailoring_service._tailor_summary_skills, _BASE_PROFILE, "JD", context)
    letter = _prompt_of(tailoring_service.generate_cover_letter, _BASE_PROFILE, "Acme", "Data Engineer", "JD",
                        json_mode=False, company_context=context)
    for prompt in (projects, summary, letter):
        assert "Acme builds route-planning software." in prompt
    assert "most relevant first" in projects and "what the company builds" in summary and "company's own words" in letter


def test_tailoring_saves_the_facts_it_used_for_the_page(db):
    application = _application(db, _BASE_PROFILE)
    brief = {"domain": "acme.com", "facts": [{"text": "Acme builds X.", "url": "https://acme.com", "own_site": True}],
             "researched_at": "2026-10-25T00:00:00"}
    with (
        patch.object(tailoring_service.company_research_service, "brief_for_posting", return_value=brief),
        patch("app.services.tailoring_service.run_multi_pass_tailoring", return_value=(_BASE_PROFILE["experience"], [], 90, [], [])) as run,
        patch("app.services.tailoring_service._tailor_summary_skills", return_value={"summary": "Data engineer.", "skills": {}}),
        patch("app.services.tailoring_service.generate_cover_letter", return_value="A letter."),
        patch("app.services.tailoring_service.score_cover_letter", return_value=80),
    ):
        tailoring_service.tailor_application(db, application.id)
    assert "Acme builds X." in run.call_args.args[3]
    from app.models import TailoredDocument
    saved = json.loads(db.query(TailoredDocument).filter_by(application_id=application.id, document_type="resume").one().content)
    assert saved["company_research"]["facts"][0]["url"] == "https://acme.com"


def test_the_application_page_shows_the_research_with_sources(db, monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app as web_app
    from app.models import TailoredDocument

    monkeypatch.setenv("APP_MODE", "showcase")
    application = _application(db, _BASE_PROFILE)
    facts = [{"text": "Acme builds route-planning software.", "url": "https://acme.com/about", "own_site": True}]
    db.add(TailoredDocument(application_id=application.id, document_type="resume",
                            content=json.dumps({**_BASE_PROFILE, "company_research": {"domain": "acme.com", "facts": facts}})))
    db.commit()
    page = TestClient(web_app).get(f"/jobs/{application.id}").text
    assert "What the tailoring knew about Acme Corp" in page and "https://acme.com/about" in page


def test_a_legal_entity_name_is_searched_by_its_common_name():
    assert research.search_name("Amazon.com Services LLC") == "Amazon"
    assert research.search_name("Acme, Inc.") == "Acme"
    assert research.search_name("Amazon Web Services, Inc.") == "Amazon Web"  # trailing generic word only
    assert research.search_name("Fireflies.ai") == "Fireflies"


def test_a_directory_site_is_not_taken_for_the_company(db):
    # 2026-10-25, live: "Amazon.com Services LLC" resolved to constrafor.com, a construction directory, and its listings for
    # unrelated firms were labelled as Amazon's own site.
    directory = [{"url": "https://www.constrafor.com/network/washington/amazoncom-services", "content": "Directory."}]
    with patch.object(research, "tavily_search", return_value=directory):
        assert research._company_domain(db, "Amazon.com Services LLC", "https://boards.greenhouse.io/x") is None
        # no company site found by search: the posting's own host is used when it carries the name
        assert research._company_domain(db, "Amazon.com Services LLC", "https://www.amazon.jobs/en/jobs/10582746") == "amazon.jobs"
    other_firm = [{"url": "https://www.constrafor.com/network/texas/mission-site-services", "content": "Mission Site Services, LLC."}]
    assert research._facts(other_firm, None, "Amazon.com Services LLC") == []


def test_the_whole_name_must_match_and_placeholders_are_skipped(db):
    assert research._is_company_host("americanexpress.com", research._key("American Express"))
    assert not research._is_company_host("americanairlines.com", research._key("American Express"))
    assert research.search_name("Integrated Resources, Inc (IRI)") == "Integrated Resources"
    company = Company(name="Apply", normalized_name="apply")
    db.add(company)
    db.commit()
    with patch.object(research, "is_tavily_configured", return_value=True), patch.object(research, "tavily_search") as search:
        assert research.brief_for(db, company)["facts"] == []
    assert search.call_count == 0


def test_the_main_com_site_wins_and_job_ads_careers_and_other_languages_are_dropped(db):
    found = [{"url": "https://nielseniq.cn/global/en/about-us", "content": "x"},
             {"url": "https://nielseniq.com/global/en/about-us", "content": "x"}]
    with patch.object(research, "tavily_search", return_value=found):
        assert research._company_domain(db, "NielsenIQ", "https://jobs.smartrecruiters.com/x") == "nielseniq.com"
    noise = [
        {"url": "https://amazon.jobs/jobs/10576167", "content": "Another Amazon opening."},
        {"url": "https://nielseniq.com/global/en/careers/university", "content": "NielsenIQ student programs."},
        {"url": "https://nielseniq.com/global/zh/insights/report", "content": "NielsenIQ 报告显示消费者"},
        {"url": "https://nielseniq.com/global/en/solutions/retail", "content": "NielsenIQ measures retail sales for brands."},
    ]
    assert [f["url"] for f in research._facts(noise, "nielseniq.com", "NielsenIQ")] == ["https://nielseniq.com/global/en/solutions/retail"]


def test_team_facts_come_first_and_are_labelled(db):
    company = Company(name="Acme", normalized_name="acme")
    db.add(company)
    db.commit()
    from app.models import JobPosting
    posting = JobPosting(company_id=company.id, company_name_raw="Acme", job_title="Data Engineer, Fleet Analytics",
                         job_description="x", source="manual", job_url="https://boards.greenhouse.io/acme/1")
    db.add(posting)
    db.commit()
    team = [{"url": "https://acme.com/blog/fleet-analytics", "content": "Acme's fleet analytics team builds ETA models."}]
    with (patch.object(research, "is_tavily_configured", return_value=True),
          patch.object(research, "_company_domain", return_value="acme.com"),
          patch.object(research, "tavily_search", side_effect=[RESULTS, RESULTS, team])):
        brief = research.brief_for_posting(db, posting)
    assert brief["facts"][0]["kind"] == "team" and brief["facts"][0]["url"].endswith("fleet-analytics")
    assert "[about this team, company site] Acme's fleet analytics team" in research.prompt_block(brief)
