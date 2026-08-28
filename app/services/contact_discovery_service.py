"""
Contact discovery -- optional, gracefully-degrading (same pattern as
Adzuna/portfolio sync: if not configured, this just returns nothing
and the user falls back to typing a contact in manually).

"Manual entry only" satisfies the no-blind-auto-email-to-guessed-
addresses rule elsewhere in this app, but doesn't help someone who
doesn't have a recruiter contact in the first place. The distinction
this module holds onto: DISCOVERING a real,
already-published person/email is not the same as GUESSING one via a
first.last@domain pattern from scratch. Every suggestion here is
labeled with its source and, for emails, a confidence score -- nothing
is ever auto-filled or auto-used. The caller always shows suggestions
to the human and routes their pick through outreach_service's normal
draft/approve/send pipeline, unchanged.

Two independent providers:
- Tavily (general web search, api.tavily.com) -- finds LinkedIn
  profiles that plausibly handle hiring for this company, and the
  company's own domain (needed for Hunter).
- Hunter.io (api.hunter.io) -- Domain Search returns real emails
  Hunter has found/verified for that company, with confidence scores
  and position titles, filtered here for recruiter/HR/talent-sounding
  roles. Deliberately using Domain Search (real found emails) rather
  than Email Finder (pattern-predicted from a name) -- closer to "not
  guessing."

Both calls are synchronous HTTP requests (not backgrounded) -- fast
enough for a live request, unlike the multi-pass LLM tailoring calls.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from urllib.parse import urlparse

import requests

from ..database import utcnow
from ..models import get_or_create_settings
from .activity_logger import log_activity

_RECRUITER_KEYWORDS = ("recruit", "talent", "people", "hr", "human resources", "hiring", "staffing", "sourc")

_NON_COMPANY_DOMAINS = ("linkedin.com", "wikipedia.org", "glassdoor.com", "indeed.com", "crunchbase.com", "google.com")

# Real recent hiring-activity posts are the single strongest "why this
# contact" signal available for free -- confirmed live (2026-08-28) that
# Tavily's Extract API surfaces a LinkedIn profile's recent Activity feed,
# and a person who just posted "we're hiring" is a far better, far more
# timely target than a generic title match.
_HIRING_ACTIVITY_KEYWORDS = (
    "hiring", "we're growing", "is growing", "join our team", "join us",
    "we're looking for", "is hiring", "open role", "open roles",
)

# Tavily Extract returns LinkedIn's public page as markdown with
# "## Section Name" headers (About/Experience/Education/Activity/...) --
# confirmed live against a real profile.
_EXTRACT_SECTION_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

_MAX_ENRICH_PER_DISCOVERY = 8  # caps Extract calls per click -- bounds latency and Tavily budget use


class BudgetExhaustedError(Exception):
    """Raised instead of letting a call go out -- distinguishes "quota
    exhausted" from "genuinely found nothing," which previously looked
    identical in the logs (both just returned an empty list)."""


def is_tavily_configured() -> bool:
    return bool(os.getenv("TAVILY_API_KEY"))


def is_hunter_configured() -> bool:
    return bool(os.getenv("HUNTER_API_KEY"))


def _reset_monthly_counter_if_needed(settings, now, used_attr: str, reset_attr: str) -> None:
    if getattr(settings, reset_attr) is None or now >= getattr(settings, reset_attr):
        setattr(settings, used_attr, 0)
        setattr(settings, reset_attr, now + timedelta(days=30))


def _consume_budget(db, provider: str) -> None:
    """Raises BudgetExhaustedError without incrementing anything if this
    month's cap is already hit; otherwise increments the counter. Called
    right before the real HTTP request, not after -- a failed request
    shouldn't count against the budget, but we also shouldn't let two
    near-simultaneous calls both slip through a stale read (acceptable
    given this is a single-operator, on-demand-click usage pattern, not
    a high-concurrency system)."""
    settings = get_or_create_settings(db)
    now = utcnow()
    used_attr, reset_attr, budget_attr = f"{provider}_calls_used_this_month", f"{provider}_month_reset_at", f"{provider}_monthly_call_budget"
    _reset_monthly_counter_if_needed(settings, now, used_attr, reset_attr)

    used = getattr(settings, used_attr)
    budget = getattr(settings, budget_attr)
    if used >= budget:
        db.commit()  # persist the reset, if one just happened, even though this call itself is refused
        log_activity(
            db,
            f"{provider.capitalize()} monthly call budget ({budget}) exhausted -- skipping this call rather "
            "than risk a real API error. Resets in 30 days from first use this period, or raise the "
            f"budget in Tunable Settings.",
            "WARNING",
        )
        raise BudgetExhaustedError(provider)

    setattr(settings, used_attr, used + 1)
    db.commit()


def tavily_search(db, query: str, max_results: int = 5) -> list:
    """Public/shared -- also used by interview_prep_service.py for light
    company research. Raises on failure (including BudgetExhaustedError);
    callers decide how to degrade (this module's own callers catch and
    fall back to an empty list)."""
    _consume_budget(db, "tavily")
    api_key = os.getenv("TAVILY_API_KEY")
    response = requests.post(
        "https://api.tavily.com/search",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={"query": query, "max_results": max_results, "search_depth": "basic"},
        timeout=15,
    )
    response.raise_for_status()
    return response.json().get("results", [])


def _find_company_domain(db, company_name: str) -> str | None:
    try:
        results = tavily_search(db, f"{company_name} official company website", max_results=3)
    except Exception:
        return None
    for r in results:
        netloc = urlparse(r.get("url", "")).netloc.replace("www.", "")
        if netloc and not any(bad in netloc for bad in _NON_COMPANY_DOMAINS):
            return netloc
    return None


def _find_linkedin_candidates(db, company_name: str) -> list:
    try:
        results = tavily_search(
            db,
            f'"{company_name}" recruiter OR "talent acquisition" OR "people team" site:linkedin.com/in',
            max_results=5,
        )
    except Exception:
        return []

    candidates = []
    for r in results:
        title = r.get("title", "")
        name = title.split(" - ")[0].strip() if " - " in title else title
        candidates.append(
            {
                "name": name or None,
                "title": title,
                "linkedin_url": r.get("url"),
                "suggested_email": None,
                "email_confidence": None,
                "source": "Tavily web search",
            }
        )
    return candidates


def _find_peer_candidates(db, company_name: str, job_title: str | None) -> list:
    """Recruiter/HR contacts (see _find_linkedin_candidates) are the
    right target for a formal "please consider my application" note.
    They are NOT who gave real, useful insider color on a real
    interview -- that came from an actual peer in a similar role, not
    HR. Same search mechanism, different target: the job title itself,
    not recruiting-department keywords."""
    if not job_title:
        return []
    try:
        results = tavily_search(db, f'"{job_title}" "{company_name}" site:linkedin.com/in', max_results=5)
    except Exception:
        return []

    candidates = []
    for r in results:
        title = r.get("title", "")
        name = title.split(" - ")[0].strip() if " - " in title else title
        candidates.append(
            {
                "name": name or None,
                "title": title,
                "linkedin_url": r.get("url"),
                "suggested_email": None,
                "email_confidence": None,
                "source": "Tavily web search (peer in a similar role)",
            }
        )
    return candidates


def _fetch_extract(url: str) -> str | None:
    """Network-only, no DB access -- safe to call from a worker thread.
    Budget consumption (a DB read+write) must happen separately, on the
    calling thread, before this runs concurrently; see discover_contacts.
    Returns None on any failure (non-200, unparseable)."""
    api_key = os.getenv("TAVILY_API_KEY")
    try:
        response = requests.post(
            "https://api.tavily.com/extract",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"urls": [url]},
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        return results[0].get("raw_content") if results else None
    except Exception:
        return None


def tavily_extract(db, url: str) -> str | None:
    """Public/shared, single-call convenience -- checks budget then
    fetches. Search only returns a title-line snippet; Extract pulls the
    full public page. Confirmed live (2026-08-28) that a LinkedIn
    profile's Extract result includes About/Experience/Education/
    Activity sections -- including real, dated recent posts, which is
    what makes compute_reason's hiring-activity signal possible. Returns
    None on any failure (budget exhausted, non-200, unparseable) -- same
    fail-soft posture as the rest of this module. NOT used internally by
    discover_contacts' concurrent enrichment pass (see _fetch_extract) --
    SQLAlchemy Sessions aren't thread-safe, so budget-checking against
    `db` can't happen inside worker threads."""
    try:
        _consume_budget(db, "tavily")
    except BudgetExhaustedError:
        return None
    return _fetch_extract(url)


def parse_extract_sections(raw_content: str | None) -> dict:
    """Splits Tavily Extract's markdown-sectioned LinkedIn output into a
    dict keyed by lowercased section name ("about", "experience",
    "education", "activity", ...). Pure parsing, no network call."""
    if not raw_content:
        return {}
    parts = _EXTRACT_SECTION_RE.split(raw_content)
    sections = {}
    for i in range(1, len(parts) - 1, 2):
        name = parts[i].strip().lower()
        body = parts[i + 1].strip()
        sections[name] = body
    return sections


def _find_hiring_activity_snippet(activity_text: str) -> str | None:
    if not activity_text:
        return None
    for line in activity_text.split("\n"):
        low = line.lower()
        if any(kw in low for kw in _HIRING_ACTIVITY_KEYWORDS):
            cleaned = line.strip("-* \t")
            if cleaned:
                return cleaned[:200]
    return None


def _find_text_overlap(text: str, candidate_values: list) -> str | None:
    if not text:
        return None
    low = text.lower()
    for value in candidate_values:
        if value and len(value) > 3 and value.lower() in low:
            return value
    return None


def compute_reason(sections: dict, profile_content: dict | None) -> str | None:
    """The mechanical "why this contact" signal -- every reason is a
    literal substring/keyword match against real Extract text and the
    candidate's own real profile, never an LLM guess or a fabricated
    detail. None means genuinely nothing found; the UI shows no reason
    rather than a made-up one. Checked in order of how actionable/
    specific the signal is: a live hiring post beats a shared school,
    which beats a shared employer."""
    hiring_snippet = _find_hiring_activity_snippet(sections.get("activity", ""))
    if hiring_snippet:
        return f'Recently posted: "{hiring_snippet}"'

    profile_content = profile_content or {}
    schools = [e.get("school") for e in profile_content.get("education", [])]
    school_match = _find_text_overlap(sections.get("education", ""), schools) or _find_text_overlap(
        sections.get("about", ""), schools
    )
    if school_match:
        return f"Also attended {school_match}"

    employers = [e.get("company") for e in profile_content.get("experience", [])]
    employer_match = _find_text_overlap(sections.get("experience", ""), employers) or _find_text_overlap(
        sections.get("about", ""), employers
    )
    if employer_match:
        return f"Previously worked at {employer_match}"

    return None


def _find_hunter_candidates(db, domain: str) -> list:
    try:
        _consume_budget(db, "hunter")
    except BudgetExhaustedError:
        return []

    api_key = os.getenv("HUNTER_API_KEY")
    try:
        response = requests.get(
            "https://api.hunter.io/v2/domain-search",
            params={"domain": domain, "api_key": api_key},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json().get("data", {})
    except Exception:
        return []

    candidates = []
    for entry in data.get("emails", []):
        position = (entry.get("position") or "").lower()
        if not any(kw in position for kw in _RECRUITER_KEYWORDS):
            continue
        first = entry.get("first_name") or ""
        last = entry.get("last_name") or ""
        candidates.append(
            {
                "name": f"{first} {last}".strip() or None,
                "title": entry.get("position"),
                "linkedin_url": entry.get("linkedin") or None,
                "suggested_email": entry.get("value"),
                "email_confidence": entry.get("confidence"),
                "source": f"Hunter.io (found on {domain})",
            }
        )
    return candidates


def discover_contacts(db, company_name: str, job_title: str | None = None, profile_content: dict | None = None) -> list:
    """Best-effort, never raises -- an empty list (neither provider
    configured, budget exhausted, or both failed) just means the caller
    falls back to manual entry. Every dict has: name, title,
    linkedin_url, suggested_email, email_confidence, source, reason
    (reason is None when nothing was found, never fabricated).

    Combines two independent searches -- recruiter/HR contacts (right
    for a formal application-boosting note) and peers in a similar role
    (right for genuine informational outreach, the kind that actually
    produced a real, useful conversation ahead of a real interview --
    see this module's git history / project memory for that story) --
    then enriches each with tavily_extract + compute_reason so the UI
    can show a real, specific reason instead of just a name and a link."""
    candidates = []

    if not is_tavily_configured():
        return candidates

    candidates.extend(_find_linkedin_candidates(db, company_name))
    candidates.extend(_find_peer_candidates(db, company_name, job_title))

    if is_hunter_configured():
        domain = _find_company_domain(db, company_name)
        if domain:
            candidates.extend(_find_hunter_candidates(db, domain))

    # The same person can legitimately surface from both searches (a
    # broad title matching both the recruiter and peer queries) --
    # dedupe by profile URL before spending an Extract call on them twice.
    seen_urls = set()
    deduped = []
    for c in candidates:
        url = c.get("linkedin_url")
        if url and url in seen_urls:
            continue
        if url:
            seen_urls.add(url)
        c.setdefault("reason", None)
        deduped.append(c)

    to_enrich = [c for c in deduped if c.get("linkedin_url")][:_MAX_ENRICH_PER_DISCOVERY]
    if to_enrich:
        # Budget consumption reads/writes GlobalSettings via `db` and
        # must happen sequentially on this thread -- SQLAlchemy Sessions
        # aren't thread-safe (same constraint _backfill_board_slugs
        # works around the same way: DB access sequential, network fetch
        # concurrent). Pre-approve spend for each URL here; a candidate
        # whose budget got refused mid-loop just gets None back (skipped
        # network call), no error surfaced.
        approved_urls = []
        for c in to_enrich:
            try:
                _consume_budget(db, "tavily")
                approved_urls.append(c["linkedin_url"])
            except BudgetExhaustedError:
                approved_urls.append(None)

        with ThreadPoolExecutor(max_workers=min(len(to_enrich), 5)) as pool:
            raw_contents = list(pool.map(lambda url: _fetch_extract(url) if url else None, approved_urls))

        for c, raw in zip(to_enrich, raw_contents):
            c["reason"] = compute_reason(parse_extract_sections(raw), profile_content)

    return deduped
