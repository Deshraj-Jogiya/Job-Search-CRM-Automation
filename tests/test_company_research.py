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
        patch.object(research, "_find_company_domain", return_value=domain),
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
        patch.object(tailoring_service.company_research_service, "brief_for", return_value=brief),
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
