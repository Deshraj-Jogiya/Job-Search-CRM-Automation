"""What the company does, from its own website first, for tailoring: which projects to pick and lead with, the
summary's closing sentence, and the cover letter's reason for applying.

One brief per company, cached on the Company row for RESEARCH_MAX_AGE_DAYS, so applying to several roles at one company
costs one round of searches (at most 3 Tavily calls: the domain, the company's own pages, and outside coverage).
Every fact keeps its source URL and whether it came from the company's own site, and the facts are shown on the
application page next to the documents they shaped. With no Tavily key, an exhausted budget or a failed search the
brief is empty and tailoring works from the job posting alone, as before."""

import json
import re
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import Company
from .contact_discovery_service import is_tavily_configured, tavily_search

RESEARCH_MAX_AGE_DAYS = 90
_MAX_FACTS = 6
_SNIPPET_CHARS = 350
# Pages that describe the company in its own words, or how its engineers work; job boards and review sites are not.
_SKIP_HOSTS = ("linkedin.", "glassdoor.", "indeed.", "greenhouse.io", "lever.co", "ashbyhq.", "wikipedia.", "crunchbase.",
               "zoominfo.", "builtin.", "levels.fyi", "teamblind.", "reddit.",
               # job boards and people-data brokers, seen live 2026-10-25: a copy of the posting or a contact list
               "careerbuilder.", "ziprecruiter.", "monster.", "simplyhired.", "dice.com", "wellfound.", "jobright.",
               "smartrecruiters.", "workable.", "rocketreach.", "leadiq.", "apollo.io", "signalhire.", "contactout.")


_LEGAL_WORDS = {"llc", "inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "gmbh",
                "holdings", "group", "lp", "llp", "na", "sa", "ag"}


def search_name(company_name: str) -> str:
    """The name people use, not the legal entity: "Amazon.com Services LLC" -> "Amazon", "Acme, Inc." -> "Acme".
    A legal entity name searched as-is found a construction directory instead of Amazon (2026-10-25)."""
    name = re.sub(r"\([^)]*\)", " ", company_name or "")  # "Integrated Resources, Inc (IRI)": the bracket is an abbreviation
    name = re.sub(r"\.(com|io|ai|co|net|org)\b", " ", name, flags=re.IGNORECASE)
    words = [w for w in re.split(r"[\s,]+", name) if w and w.strip(".").lower() not in _LEGAL_WORDS]
    # "Amazon Services" -> "Amazon": a generic trailing word left after the legal suffix is part of the entity name
    while len(words) > 1 and words[-1].lower() in ("services", "service", "solutions", "technologies", "usa", "us"):
        words.pop()
    return " ".join(words).strip() or (company_name or "").strip()


# Names a posting carries when the real employer was not captured; researching them finds nothing about the employer.
_PLACEHOLDER_NAMES = {"apply", "company", "confidential", "unknown", "hiring", "employer", "stealth", "n/a"}


def _key(company_name: str) -> str:
    """The whole common name as letters and digits ("americanexpress"): what a company's own host and a result about
    it must contain. The whole name, not the first word, so "American Express" does not accept americanairlines.com.
    Empty for a placeholder or too-short name, which turns research off."""
    name = search_name(company_name)
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    return "" if name.lower() in _PLACEHOLDER_NAMES or len(key) < 3 else key


def _host(url: str) -> str:
    return urlparse(url or "").netloc.lower().removeprefix("www.")


def _is_company_host(host: str, key: str) -> bool:
    flat = re.sub(r"[^a-z0-9]", "", host)
    return bool(key) and bool(host) and key in flat and not any(skip in host for skip in _SKIP_HOSTS)


def _mentions(text: str, company_name: str) -> bool:
    """The result names the company: its common name as a phrase, or squashed together ("NielsenIQ", "nielseniq")."""
    lowered = text.lower()
    return search_name(company_name).lower() in lowered or _key(company_name) in re.sub(r"[^a-z0-9]", "", lowered)


def _company_domain(db: Session, company_name: str, posting_url: str | None) -> str | None:
    """The company's own domain, only when its address contains the company's name: the best match from a search
    (a .com over a regional or jobs site), else the posting's own host when it carries the name (amazon.jobs)."""
    core = _key(company_name)
    if not core:
        return None
    host = _host(posting_url)
    try:
        results = tavily_search(db, f"{search_name(company_name)} official company website", max_results=5)
    except Exception:
        results = []
    found = [h for h in (_host(r.get("url", "")) for r in results) if _is_company_host(h, core)]
    # the main .com site over a regional one (NielsenIQ resolved to nielseniq.cn) and over a jobs site such as
    # amazon.jobs, which describes openings rather than the company; the posting's own host is the fallback
    found.sort(key=lambda h: (not h.endswith(".com"), "jobs" in h or "careers" in h))
    if found:
        return found[0]
    return host if _is_company_host(host, core) else None


# Pages that are another job ad, a careers or student-recruiting page, or not in English: none says what the company
# builds for its customers. Seen live on amazon.jobs (other openings) and nielseniq.cn (careers, Chinese reports).
_SKIP_PATH_RE = re.compile(r"/(jobs?|careers?|university|students?|internships?|zh|ja|ko|cn)(/|$)|/jobs?/\d+", re.IGNORECASE)


def _english(text: str) -> bool:
    return sum(ord(ch) > 127 for ch in text) <= len(text) * 0.1


def _facts(results: list, domain: str | None, company_name: str, kind: str = "company") -> list[dict]:
    key = _key(company_name)
    facts, seen = [], set()
    for r in results:
        url, text = r.get("url", ""), " ".join((r.get("content") or "").split())
        host = _host(url)
        if not text or url in seen or any(skip in host for skip in _SKIP_HOSTS):
            continue
        if _SKIP_PATH_RE.search(urlparse(url).path) or not _english(text):
            continue
        if not key or not (_is_company_host(host, key) or _mentions(text, company_name)):
            continue  # not about this company (a directory listing for another firm, a similarly named one)
        seen.add(url)
        # any address carrying the company's name is its own (nielseniq.com when the search found nielseniq.cn)
        own = _is_company_host(host, key) or (bool(domain) and (host == domain or host.endswith("." + domain)))
        facts.append({"text": text[:_SNIPPET_CHARS], "url": url, "own_site": own, "kind": kind})
    # the company's own words first
    return sorted(facts, key=lambda f: not f["own_site"])[:_MAX_FACTS]


def _research(db: Session, company_name: str, posting_url: str | None = None) -> dict:
    name = search_name(company_name)
    domain = _company_domain(db, company_name, posting_url)
    results = []
    queries = []
    if domain:
        queries.append(f"site:{domain} {name} products customers mission engineering")
    queries.append(f"{name} company products customers technology engineering blog")
    for query in queries:
        try:
            results += tavily_search(db, query, max_results=6)
        except Exception:
            break  # budget exhausted or the search failed: keep whatever was found
    facts = _facts(results, domain, company_name)
    own_hosts = sorted({_host(f["url"]) for f in facts if f["own_site"]}, key=lambda h: (not h.endswith(".com"), len(h)))
    return {"domain": own_hosts[0] if own_hosts else domain, "facts": facts}


_MAX_TEAM_FACTS = 2


def team_facts(db: Session, company_name: str, job_title: str, domain: str | None) -> list[dict]:
    """What the team or organisation behind this posting does, one search, not cached: at a large company the
    company-wide brief says little about the team ("Amazon" vs "Amazon Security Experience"). Never raises."""
    if not is_tavily_configured() or not _key(company_name) or not (job_title or "").strip():
        return []
    try:
        results = tavily_search(db, f"{search_name(company_name)} {job_title} team what it builds", max_results=6)
    except Exception:
        return []
    return _facts(results, domain, company_name, kind="team")[:_MAX_TEAM_FACTS]


def brief_for_posting(db: Session, posting) -> dict:
    """The company brief (cached) plus what the posting's own team does (fresh), for one tailoring run."""
    brief = brief_for(db, posting.company, posting_url=posting.job_url)
    team = team_facts(db, posting.company.name if posting.company else posting.company_name_raw, posting.job_title,
                      brief.get("domain"))
    known = {f["url"] for f in brief.get("facts", [])}
    return {**brief, "facts": [f for f in team if f["url"] not in known] + brief.get("facts", [])}


def brief_for(db: Session, company: Company | None, refresh: bool = False, posting_url: str | None = None) -> dict:
    """{"domain", "facts": [{"text", "url", "own_site"}], "researched_at"}; never raises."""
    empty = {"domain": None, "facts": [], "researched_at": None}
    if company is None:
        return empty
    fresh = company.researched_at and utcnow() - company.researched_at < timedelta(days=RESEARCH_MAX_AGE_DAYS)
    if company.research_brief and fresh and not refresh:
        return {**json.loads(company.research_brief), "researched_at": company.researched_at.isoformat()}
    if not is_tavily_configured() or not _key(company.name):
        return empty
    try:
        brief = _research(db, company.name, posting_url)
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
    def label(f):
        place = "company site" if f["own_site"] else "outside source"
        return f"about this team, {place}" if f.get("kind") == "team" else place
    lines = [f"- [{label(f)}] {f['text']}" for f in brief["facts"]]
    return ("COMPANY CONTEXT (research on the employer; the company-site lines are its own words). Use it only to choose "
            "emphasis and to say why this company; never present it as the candidate's experience, and state no company "
            "fact that is not here or in the job description:\n" + "\n".join(lines))
