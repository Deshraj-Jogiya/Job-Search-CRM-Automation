"""Shared title-matching logic for the direct ATS board sources
(Greenhouse, Lever, Ashby) -- these poll a company's whole public board
and filter locally by title, unlike Adzuna/LinkedIn which pass keywords
to a real search API. Centralized here since all three sources need the
identical keyword-match plus seniority-exclusion check.

Also centralizes location filtering, used by all five intake sources
(the three direct-board sources have no location search param at all,
and Adzuna/LinkedIn's own location search param is a fuzzy hint, not a
guarantee) -- see location_allowed.
"""

import re

from ...database import SessionLocal
from ...models import LocationExclusion, SeniorityExclusion


def matches_keywords(title: str, keywords: list[str]) -> bool:
    lower_title = title.lower()
    return any(kw.lower() in lower_title for kw in keywords)


def get_active_seniority_exclusions() -> list[str]:
    db = SessionLocal()
    try:
        rows = db.query(SeniorityExclusion).filter(SeniorityExclusion.is_active == True).all()  # noqa: E712
        return [r.term for r in rows]
    finally:
        db.close()


def matches_seniority_exclusion(title: str, exclusions: list[str]) -> bool:
    if not exclusions:
        return False
    lower_title = title.lower()
    return any(term.lower() in lower_title for term in exclusions)


def posting_matches(title: str, keywords: list[str], exclusions: list[str]) -> bool:
    """A company's own board has no seniority filter -- exclusions are
    the only thing standing between an early-career profile and every
    Staff/Director/VP posting on that board."""
    return matches_keywords(title, keywords) and not matches_seniority_exclusion(title, exclusions)


def get_active_location_exclusions() -> list[str]:
    db = SessionLocal()
    try:
        rows = db.query(LocationExclusion).filter(LocationExclusion.is_active == True).all()  # noqa: E712
        return [r.term for r in rows]
    finally:
        db.close()


def location_allowed(location: str | None, exclusions: list[str]) -> bool:
    """Fails open: a posting with no location text at all (a source that
    didn't return one, or a genuinely location-less listing) is allowed
    through rather than silently dropped -- exclusion only ever fires on
    a positive match against a real location string. Uses word-boundary
    matching, not raw substring, so a short exclusion term (e.g. a
    country name that's also a common word fragment) can't false-positive
    against an unrelated city name."""
    if not location or not exclusions:
        return True
    for term in exclusions:
        if re.search(rf"\b{re.escape(term)}\b", location, re.IGNORECASE):
            return False
    return True
