"""What the company does, from its own website first, for tailoring: which projects to pick and lead with, the
summary's closing sentence, and the cover letter's reason for applying.

One brief per company, cached on the Company row for RESEARCH_MAX_AGE_DAYS, so applying to several roles at one company
costs one round of searches (at most 3 Tavily calls: the domain, the company's own pages, and outside coverage).
Every fact keeps its source URL and whether it came from the company's own site, and the facts are shown on the
application page next to the documents they shaped. With no Tavily key, an exhausted budget or a failed search the
brief is empty and tailoring works from the job posting alone, as before."""

import json
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Company
from .contact_discovery_service import _find_company_domain, is_tavily_configured, tavily_search

RESEARCH_MAX_AGE_DAYS = 90
_MAX_FACTS = 6
_SNIPPET_CHARS = 350
# Pages that describe the company in its own words, or how its engineers work; job boards and review sites are not.
_SKIP_HOSTS = ("linkedin.", "glassdoor.", "indeed.", "greenhouse.io", "lever.co", "ashbyhq.", "wikipedia.", "crunchbase.",
               "zoominfo.", "builtin.", "levels.fyi", "teamblind.", "reddit.")


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def _facts(results: list, domain: str | None) -> list[dict]:
    facts, seen = [], set()
    for r in results:
        url, text = r.get("url", ""), " ".join((r.get("content") or "").split())
        host = _host(url)
        if not text or url in seen or any(skip in host for skip in _SKIP_HOSTS):
            continue
        seen.add(url)
        own = bool(domain) and (host == domain or host.endswith("." + domain))
        facts.append({"text": text[:_SNIPPET_CHARS], "url": url, "own_site": own})
    # the company's own words first
    return sorted(facts, key=lambda f: not f["own_site"])[:_MAX_FACTS]


def _research(db: Session, company_name: str) -> dict:
    domain = _find_company_domain(db, company_name)
    results = []
    queries = []
    if domain:
        queries.append(f"site:{domain} {company_name} products customers mission engineering")
    queries.append(f"{company_name} company products customers technology engineering blog")
    for query in queries:
        try:
            results += tavily_search(db, query, max_results=6)
        except Exception:
            break  # budget exhausted or the search failed: keep whatever was found
    return {"domain": domain, "facts": _facts(results, domain)}


def brief_for(db: Session, company: Company | None, refresh: bool = False) -> dict:
    """{"domain", "facts": [{"text", "url", "own_site"}], "researched_at"}; never raises."""
    empty = {"domain": None, "facts": [], "researched_at": None}
    if company is None:
        return empty
    fresh = company.researched_at and utcnow() - company.researched_at < timedelta(days=RESEARCH_MAX_AGE_DAYS)
    if company.research_brief and fresh and not refresh:
        return {**json.loads(company.research_brief), "researched_at": company.researched_at.isoformat()}
    if not is_tavily_configured():
        return empty
    try:
        brief = _research(db, company.name)
    except Exception:
        return empty
    if not brief["facts"]:
        return {**empty, "domain": brief["domain"]}  # nothing usable: try again next time instead of caching an empty brief
    company.website_domain = brief["domain"] or company.website_domain
    company.research_brief = json.dumps(brief)
    company.researched_at = utcnow()
    db.commit()
    return {**brief, "researched_at": company.researched_at.isoformat()}


def prompt_block(brief: dict) -> str:
    """The brief as prompt text; empty when there is nothing to use."""
    if not brief.get("facts"):
        return ""
    lines = [f"- [{'company site' if f['own_site'] else 'outside source'}] {f['text']}" for f in brief["facts"]]
    return ("COMPANY CONTEXT (research on the employer; the company-site lines are its own words). Use it only to choose "
            "emphasis and to say why this company; never present it as the candidate's experience, and state no company "
            "fact that is not here or in the job description:\n" + "\n".join(lines))
