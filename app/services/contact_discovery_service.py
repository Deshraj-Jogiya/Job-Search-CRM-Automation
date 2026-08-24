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
from datetime import timedelta
from urllib.parse import urlparse

import requests

from ..database import utcnow
from ..models import get_or_create_settings
from .activity_logger import log_activity

_RECRUITER_KEYWORDS = ("recruit", "talent", "people", "hr", "human resources", "hiring", "staffing", "sourc")

_NON_COMPANY_DOMAINS = ("linkedin.com", "wikipedia.org", "glassdoor.com", "indeed.com", "crunchbase.com", "google.com")


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


def discover_contacts(db, company_name: str) -> list:
    """Best-effort, never raises -- an empty list (neither provider
    configured, budget exhausted, or both failed) just means the caller
    falls back to manual entry. Every dict has: name, title,
    linkedin_url, suggested_email, email_confidence, source."""
    candidates = []

    if not is_tavily_configured():
        return candidates

    candidates.extend(_find_linkedin_candidates(db, company_name))

    if is_hunter_configured():
        domain = _find_company_domain(db, company_name)
        if domain:
            candidates.extend(_find_hunter_candidates(db, domain))

    return candidates
