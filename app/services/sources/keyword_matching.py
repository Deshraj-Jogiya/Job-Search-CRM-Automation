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
    against an unrelated city name.

    Deliberately deny-list based (only excludes on a positive match, not
    "must positively contain a US/Canada signal") since this runs at
    ingestion time, where the discovery net should stay wide -- a city-only
    location string with no exclusion-list term at all (e.g. "Coimbatore,
    Tamil Nadu" before India's own city names are enumerated) is meant to
    still get caught by a real human reviewing the posting later, not
    silently dropped from discovery. See location_eligible_for_scoring for
    the stricter, allow-list gate used right before a real paid/subscription
    LLM call, where fail-open is no longer the safe default."""
    if not location or not exclusions:
        return True
    for term in exclusions:
        if re.search(rf"\b{re.escape(term)}\b", location, re.IGNORECASE):
            return False
    return True


# US state names/abbreviations and Canadian province names/major cities,
# used as positive signals by location_eligible_for_scoring. Two-letter
# abbreviations are matched with strict word-boundaries and required to be
# comma-adjacent (see _HAS_US_STATE_ABBR) so a real word like "IN" (Indiana)
# can't false-positive inside ordinary sentence text.
_US_STATE_NAMES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana", "Maine",
    "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi",
    "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio",
    "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island", "South Carolina",
    "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia",
    "Washington", "West Virginia", "Wisconsin", "Wyoming",
]
_US_STATE_ABBR = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
]
_CANADA_TERMS = [
    "Canada", "Ontario", "Quebec", "British Columbia", "Alberta", "Manitoba",
    "Saskatchewan", "Nova Scotia", "New Brunswick", "Newfoundland",
    "Prince Edward Island", "Toronto", "Vancouver", "Montreal", "Ottawa",
    "Calgary", "Edmonton", "Winnipeg",
]
_US_CANADA_PHRASE_TERMS = (
    ["United States", "USA", "U.S.A", "U.S.", "Remote - US", "Remote, US",
     "US-Remote", "US Remote"]
    + _US_STATE_NAMES
    + _CANADA_TERMS
)
_US_CANADA_PHRASE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _US_CANADA_PHRASE_TERMS) + r")\b",
    re.IGNORECASE,
)
# WA, in isolation, is common enough (and short enough) that word-boundary
# matching alone isn't safe -- require it to sit where a real address puts
# a state code: right after a comma (optionally with a space), e.g.
# "Seattle, WA" or "Seattle,WA". This still matches every real multi-segment
# location string seen in this project's actual JobPosting.location data.
_US_STATE_ABBR_RE = re.compile(
    r",\s*(?:" + "|".join(_US_STATE_ABBR) + r")\b", re.IGNORECASE
)


def location_eligible_for_scoring(location: str | None, deny_terms: list[str] | None = None) -> bool | None:
    """Gate for the point where a real LLM call is about to be spent
    (scoring/tailoring), not ingestion.

    Real incident, 2026-09-24: the first version of this function was pure
    allow-list (location must positively contain a US/Canada signal, else
    excluded) and was run once against the live backlog -- it incorrectly
    excluded 458 applications, most of which were real US postings with
    location strings this project's own sources genuinely produce and that
    no reasonable allow-list can fully enumerate: a bare "US" with no
    state, a bare well-known city with no state ("San Francisco"), or a
    "City, County Name" shape some ATSes use instead of a state ("Chantilly,
    Fairfax County", "Plano, Collin County"). An allow-list that fails
    closed on anything it doesn't recognize will always be too aggressive
    for real, messy location data.

    This version is deny-list-first, exactly like location_allowed, so it
    only ever excludes on a real, positive match against a known non-US/
    non-Canada country or region term (reuses the same maintained
    LocationExclusion list via deny_terms, falling back to
    intake_service's own default list if the caller doesn't pass one) --
    catching what location_allowed's own ingestion-time check might have
    missed (e.g. a location string with a country token that wasn't yet in
    the exclusion list when the posting was first ingested). The
    allow-list check (US/Canada signal) only ever promotes an ambiguous
    case to a confirmed True; it is never used to force a False. Anything
    that matches neither list is a genuine "can't tell" and returns None,
    which callers must treat as "let it through" -- the same fail-open
    default as location_allowed, not an implicit exclude.

    Splits are implicit: deny/allow terms are checked with simple
    substring/word-boundary search across the whole string, so multi-
    location strings like "India - Bengaluru; India - Pune" (deny match)
    or "New York City, NY | Seattle, WA; San Francisco, CA" (allow match)
    both resolve correctly without needing to parse segments by hand."""
    if not location or not location.strip():
        return None
    text = location.strip()
    terms = deny_terms if deny_terms is not None else get_active_location_exclusions()
    for term in terms:
        if re.search(rf"\b{re.escape(term)}\b", text, re.IGNORECASE):
            return False
    if _US_CANADA_PHRASE_RE.search(text) or _US_STATE_ABBR_RE.search(text):
        return True
    return None
