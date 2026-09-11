"""
Per-posting wage-level fit -- fills the gap Company.max_wage_level_15xx
alone leaves open (see FUTURE.md's "Wage-level fit is company-level,
not per-posting"): that field is an employer's HISTORICAL highest
DOL-filed wage level across every Computer/Mathematical filing they've
ever made, not what a SPECIFIC posting actually offers.

Three real, honest limitations, stated up front rather than papered
over:
  1. Most JDs don't state a salary at all -- salary_parser.py only
     extracts one when the text is genuinely unambiguous, never
     inferred from title/seniority/company size. None here is the
     common, expected case, not a failure.
  2. This app tracks one candidate's job search (data engineering
     roles), not a general multi-occupation platform -- rather than
     classifying each posting's exact SOC code (its own real, unbuilt
     problem), every lookup uses one configured target occupation
     (GlobalSettings.target_soc_code).
  3. Matching a posting's raw location string to an OEWS area title is
     a plain substring match on the OEWS-loaded areas for that SOC
     code, not a real geocoder -- "Remote" or an area OEWS doesn't
     cover (or that hasn't been loaded, see SETUP.md) resolves to no
     match, falling back to the company-level signal exactly as
     scoring already did before this existed.

Whenever a real per-posting wage level IS computed, scoring_service.py
prefers it over Company.max_wage_level_15xx -- see
_wage_level_fit_component.
"""

import re

from sqlalchemy.orm import Session

from ..models import GlobalSettings, JobPosting, OewsWage
from .salary_parser import parse_salary_range

_LOCATION_RE = re.compile(r"^\s*([A-Za-z .'-]+?)\s*,\s*([A-Za-z]{2})\b")


def _extract_city_state(location: str) -> tuple[str, str] | None:
    """Pulls a plain "City, ST" pair out of a raw location string --
    "Austin, TX", "Austin, TX (Remote)", "Austin, TX 78701" all match;
    "Remote" or an empty/unstructured string returns None."""
    if not location:
        return None
    match = _LOCATION_RE.match(location)
    if not match:
        return None
    return match.group(1).strip(), match.group(2).upper()


def find_oews_area_title(db: Session, location: str, soc_code: str) -> str | None:
    """Finds the OEWS area_title (for this SOC code) whose name
    contains the posting's city and ends in the same state -- e.g.
    "Austin, TX" matches an area_title of "Austin-Round Rock, TX".
    Returns None on no structured city/state, or no loaded OEWS area
    matching both -- never a guess across states or a fuzzy score-based
    pick among several candidates (the first exact-city-and-state match
    wins; a genuinely ambiguous location isn't resolved silently)."""
    parsed = _extract_city_state(location)
    if not parsed:
        return None
    city, state = parsed

    candidates = (
        db.query(OewsWage.area_title)
        .filter(OewsWage.soc_code == soc_code, OewsWage.area_title.ilike(f"%, {state}"))
        .distinct()
        .all()
    )
    city_lower = city.lower()
    for (area_title,) in candidates:
        # area_title's own city-portion (before the trailing ", ST") is
        # hyphen-joined multiple cities, e.g. "Austin-Round Rock, TX" --
        # split on hyphens so "Austin" matches as a whole city name, not
        # a bare substring of some unrelated longer word.
        area_cities = area_title.rsplit(",", 1)[0].lower()
        if city_lower in [c.strip() for c in area_cities.split("-")]:
            return area_title
    return None


def compute_per_posting_wage_level(db: Session, posting: JobPosting, settings: GlobalSettings) -> dict:
    """Resolves this posting's offered salary (manual entry always wins
    over a parsed one -- see apply_wage_level_to_posting) and, when both
    a salary and an OEWS area match exist for the configured SOC code,
    classifies it via wages.wage_level_for(). Returns
    {"salary_min", "salary_max", "salary_source", "wage_level"} --
    wage_level is None whenever any piece of that chain is missing,
    which is the honest, common case, not treated as an error."""
    from ..ingest.wages import wage_level_for  # local import avoids a circular import (ingest imports services)

    if posting.offered_salary_source == "manual" and posting.offered_salary_min is not None:
        salary_min, salary_max, source = posting.offered_salary_min, posting.offered_salary_max, "manual"
    else:
        parsed = parse_salary_range(posting.job_description)
        if parsed:
            salary_min, salary_max, source = parsed[0], parsed[1], "parsed"
        else:
            salary_min, salary_max, source = None, None, None

    wage_level = None
    if salary_min is not None:
        area_title = find_oews_area_title(db, posting.location, settings.target_soc_code)
        if area_title:
            offered_avg = (salary_min + salary_max) / 2
            wage_level = wage_level_for(db, settings.target_soc_code, area_title, offered_avg)

    return {
        "salary_min": salary_min, "salary_max": salary_max, "salary_source": source, "wage_level": wage_level,
    }


def apply_wage_level_to_posting(db: Session, posting: JobPosting, settings: GlobalSettings) -> None:
    """Computes and persists the per-posting fields onto `posting` --
    does NOT commit (matches this codebase's convention of leaving the
    commit boundary to the caller, e.g. intake_service.py's batch
    ingestion loop). A prior MANUAL entry (offered_salary_source ==
    "manual") is never overwritten by a fresh parse -- see
    compute_per_posting_wage_level."""
    result = compute_per_posting_wage_level(db, posting, settings)
    posting.offered_salary_min = result["salary_min"]
    posting.offered_salary_max = result["salary_max"]
    posting.offered_salary_source = result["salary_source"]
    posting.wage_level_per_posting = result["wage_level"]


def set_manual_salary(db: Session, posting: JobPosting, settings: GlobalSettings, salary_min: int, salary_max: int) -> None:
    """The manual-override path (e.g. a Jobs-page form) -- always wins
    over any future re-parse, per compute_per_posting_wage_level's own
    precedence rule. Commits, matching this codebase's convention for a
    single explicit user action (contrast with apply_wage_level_to_posting's
    batch-ingestion use)."""
    posting.offered_salary_min = salary_min
    posting.offered_salary_max = salary_max
    posting.offered_salary_source = "manual"
    result = compute_per_posting_wage_level(db, posting, settings)
    posting.wage_level_per_posting = result["wage_level"]
    db.commit()
