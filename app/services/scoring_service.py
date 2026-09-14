"""
Mechanical score components layered onto the existing LLM match score.
See WEIGHTING.md for the full rationale and the exact mapping from the
original spec's weights onto this codebase's real data -- short
version: this app's match_score has always been a single opaque
LLM judgment (0-100), never a set of named weighted sub-scores, so
"extending" it means folding that whole number in as one 30-point
component ("AI Profile Fit") alongside six new, purely mechanical
components computed from data intake/Phase 1 already collect --
Company.tier, Company.max_wage_level_15xx, JobPosting.worksite_ambiguous/
sponsorship_signal/job_title/first_seen_at, SearchKeyword/
SeniorityExclusion. Every component's raw value/weight/contribution is
recorded in score_breakdown so the UI can render exactly why an
application scored what it did (see application_detail.html /
queue.html's breakdown table).

role_title_match and posting_recency (added after an explicit audit
against the original spec found both missing, see WEIGHTING.md's
"Two components added after a real audit" section) reuse existing
profile-derived data rather than inventing new inputs: SearchKeyword/
SeniorityExclusion are the same tables intake_service.py already
derives from the candidate's own profile to build search queries, and
the recency buckets scale off GlobalSettings.stale_posting_threshold_days,
the same already-user-configurable cutoff staleness_flag uses --
deliberately not a second hardcoded day count.

Deliberately separate from matching_service.py's LLM call:
recompute_score_breakdown() below is pure and free -- no LLM, no
network call -- so it can run on every posting at intake/backfill
time, safe to call in bulk. matching_service.score_application() calls
it once at the end of a real (paid) LLM scoring pass so score_breakdown
stays in sync with the latest match_score; the backfill CLI
(ingest/cli.py's rescore-all) calls it directly across every
application without ever touching the LLM, since most of what changed
between "yesterday's tier" and "today's tier" doesn't require redoing
profile-fit analysis.
"""

from datetime import datetime

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import (
    Company,
    GlobalSettings,
    JobApplication,
    JobPosting,
    SearchKeyword,
    SeniorityExclusion,
    get_or_create_settings,
)

_SPONSORSHIP_HISTORY_WEIGHT = 30
_WAGE_LEVEL_FIT_WEIGHT = 20
_ROLE_TITLE_MATCH_WEIGHT = 12
_WORKSITE_CLARITY_WEIGHT = 6
_POSTING_RECENCY_WEIGHT = 2
_SPONSORSHIP_SIGNAL_BONUS_WEIGHT = 10
_AI_PROFILE_FIT_WEIGHT = 30  # the folded-in original match_score, see module docstring
# Base components (ai_profile_fit + sponsorship_history + wage_level_fit +
# role_title_match + worksite_clarity + posting_recency) sum to 100 --
# sponsorship_signal_bonus is a genuine +10 on top, clamped at 100 total.
# See WEIGHTING.md for why worksite_clarity's weight moved from 10 to 6
# to make room: it's the most heuristic of the pre-existing components
# (its own docstring already called it an approximation, not precise
# geolocation), and the user's own ordering placed role/title match
# above it and posting recency below it -- not an arbitrary cut.

# Per-tier/per-level point values as RATIOS of their component's own
# weight, not fixed numbers -- b2's Tier 2 evidence can propose tuning
# _SPONSORSHIP_HISTORY_WEIGHT/_WAGE_LEVEL_FIT_WEIGHT within
# config/adaptation.yaml's scoring_weights bounds (see
# recompute_score_breakdown below); the ratios keep each tier's/level's
# relative standing intact when that happens. At the default weight
# (30/20) these ratios reproduce the exact original point values
# (30/18/8/0/4 and 20/15/8/3/6) -- see WEIGHTING.md.
_WAGE_LEVEL_FIT_RATIO = {"IV": 1.0, "III": 0.75, "II": 0.4, "I": 0.15, "Below Level I": 0.0}
_WAGE_LEVEL_FIT_UNKNOWN_RATIO = 0.3

_SPONSORSHIP_HISTORY_RATIO_BY_TIER = {"A": 1.0, "B": 0.6, "C": 8 / 30, "X": 0.0}
_SPONSORSHIP_HISTORY_UNKNOWN_RATIO = 4 / 30


def _sponsorship_history_component(company: Company | None, weight: float = _SPONSORSHIP_HISTORY_WEIGHT) -> dict:
    if company is None:
        raw, contribution = None, round(weight * _SPONSORSHIP_HISTORY_UNKNOWN_RATIO)
    elif company.is_cap_exempt:
        # A full-weight floor regardless of tier -- year-round filing
        # ability outside the lottery is itself a strong positive signal,
        # not just a tiebreaker within a tier (see company_tier.py).
        raw, contribution = f"{company.tier or '?'} (cap-exempt)", round(weight)
    elif company.tier in _SPONSORSHIP_HISTORY_RATIO_BY_TIER:
        raw, contribution = company.tier, round(weight * _SPONSORSHIP_HISTORY_RATIO_BY_TIER[company.tier])
    else:
        raw, contribution = None, round(weight * _SPONSORSHIP_HISTORY_UNKNOWN_RATIO)
    return {"raw": raw, "weight": weight, "contribution": contribution}


def _wage_level_fit_component(
    company: Company | None, weight: float = _WAGE_LEVEL_FIT_WEIGHT, posting: JobPosting | None = None,
) -> dict:
    """Prefers posting.wage_level_per_posting (see wage_level_service.py
    -- this specific posting's actual offered/parsed salary classified
    against real OEWS data for its own location) over
    company.max_wage_level_15xx (that employer's HISTORICAL highest
    DOL-filed wage level across every Computer/Mathematical filing they
    have ever made) whenever a per-posting value was actually computed.
    Falls back to the company-level signal otherwise -- the common
    case, since most JDs don't state a salary at all (see
    salary_parser.py's own docstring)."""
    level = (posting.wage_level_per_posting if posting else None) or (company.max_wage_level_15xx if company else None)
    ratio = _WAGE_LEVEL_FIT_RATIO.get(level, _WAGE_LEVEL_FIT_UNKNOWN_RATIO)
    return {"raw": level, "weight": weight, "contribution": round(weight * ratio)}


_WORKSITE_CLARITY_RATIO_SPECIFIC = 1.0
_WORKSITE_CLARITY_RATIO_REMOTE_OR_UNSET = 0.6
_WORKSITE_CLARITY_RATIO_AMBIGUOUS = 0.0


def _worksite_clarity_component(posting: JobPosting | None, weight: float = _WORKSITE_CLARITY_WEIGHT) -> dict:
    """No exact "is this a specific US metro" algorithm was specified --
    approximated here from JobPosting.location (a raw string from the
    source, not geocoded) plus the mechanical worksite_ambiguous flag
    (sponsorship_signals.py). A bare "Remote" with no city named scores
    the middle value, same as "no location given at all" -- both are
    real ambiguity, just not the explicit red-flag phrasing
    worksite_ambiguous catches. Documented as an approximation, not
    claimed as precise geolocation. Ratio-based (not fixed point values)
    so the weight can move -- see the b2 sponsorship_history/wage_level_fit
    pattern -- without each branch needing its own edit."""
    if posting is None:
        return {"raw": None, "weight": weight, "contribution": round(weight * 0.5)}
    if posting.worksite_ambiguous:
        return {"raw": posting.location, "weight": weight, "contribution": round(weight * _WORKSITE_CLARITY_RATIO_AMBIGUOUS)}

    location = (posting.location or "").strip()
    if not location:
        ratio = _WORKSITE_CLARITY_RATIO_REMOTE_OR_UNSET
    elif "," in location:
        # A real "City, ST" (or similar) shape -- specific enough to
        # count as a named metro rather than a bare "Remote".
        ratio = _WORKSITE_CLARITY_RATIO_SPECIFIC
    elif "remote" in location.lower():
        ratio = _WORKSITE_CLARITY_RATIO_REMOTE_OR_UNSET
    else:
        ratio = _WORKSITE_CLARITY_RATIO_SPECIFIC
    return {"raw": location or None, "weight": weight, "contribution": round(weight * ratio)}


_ROLE_TITLE_MATCH_NOT_CONFIGURED_RATIO = 0.5  # no active SearchKeyword yet -- neutral, not a penalty for data we weren't given
_ROLE_TITLE_MATCH_NO_HIT_RATIO = 0.35  # keywords ARE configured but none matched -- a real but soft signal, substring matching against free-text titles is inherently imprecise
_ROLE_TITLE_MATCH_SENIORITY_EXCLUDED_RATIO = 0.0  # title contains a term the candidate explicitly excluded -- a strong, direct negative signal


def _role_title_match_component(
    posting: JobPosting | None,
    weight: float = _ROLE_TITLE_MATCH_WEIGHT,
    active_keywords: list[str] | None = None,
    active_seniority_exclusions: list[str] | None = None,
) -> dict:
    """Mechanical, free -- reuses the candidate's own real target-title
    list (SearchKeyword) and seniority exclusions (SeniorityExclusion),
    the same profile-derived data intake_service.py already uses to
    build search queries (see ensure_intake_targeting). Needed because
    several real sources pull EVERY posting from a company/board with no
    title filter at all (every direct-ATS source, plus the 4 remote-job
    boards) -- intake_service.py doesn't enforce SeniorityExclusion as a
    hard filter on those, so a posting can reach scoring despite
    matching a term the candidate explicitly excluded, with nothing
    upstream ever having checked. Deliberately substring-based, same
    posture as sponsorship_signals.py -- no fuzzy/semantic matching, so
    a non-match is a soft signal, not proof of a bad fit."""
    title = (posting.job_title or "").lower() if posting else ""

    for term in active_seniority_exclusions or []:
        if term and term.lower() in title:
            return {
                "raw": f"excluded seniority term: {term}",
                "weight": weight,
                "contribution": round(weight * _ROLE_TITLE_MATCH_SENIORITY_EXCLUDED_RATIO),
            }

    if not active_keywords:
        return {"raw": None, "weight": weight, "contribution": round(weight * _ROLE_TITLE_MATCH_NOT_CONFIGURED_RATIO)}

    matched = next((kw for kw in active_keywords if kw and kw.lower() in title), None)
    if matched:
        return {"raw": matched, "weight": weight, "contribution": round(weight)}
    return {"raw": None, "weight": weight, "contribution": round(weight * _ROLE_TITLE_MATCH_NO_HIT_RATIO)}


_POSTING_RECENCY_RATIO_FRESH = 1.0
_POSTING_RECENCY_RATIO_RECENT = 0.7
_POSTING_RECENCY_RATIO_AGING = 0.4
_POSTING_RECENCY_RATIO_STALE = 0.15
_DEFAULT_STALE_THRESHOLD_DAYS = 45  # matches GlobalSettings.stale_posting_threshold_days's own column default


def _posting_recency_component(
    posting: JobPosting | None,
    settings: GlobalSettings | None,
    weight: float = _POSTING_RECENCY_WEIGHT,
    now: datetime | None = None,
) -> dict:
    """Buckets scaled off GlobalSettings.stale_posting_threshold_days --
    the same, already-user-configurable cutoff intake_service.py's
    _flag_stale_postings already uses -- instead of a second hardcoded
    day count. first_seen_at (this app's own earliest-observed
    timestamp, preserved across reposts -- see JobPosting's own
    docstring) is used over created_at as the real "how long has this
    listing existed" signal. Buckets, not a continuous decay curve,
    matching every other component in this module's style."""
    if posting is None or posting.first_seen_at is None:
        return {"raw": None, "weight": weight, "contribution": round(weight * _POSTING_RECENCY_RATIO_AGING)}

    threshold_days = (settings.stale_posting_threshold_days if settings else None) or _DEFAULT_STALE_THRESHOLD_DAYS
    now = now or utcnow()
    age_days = (now - posting.first_seen_at).total_seconds() / 86400

    if posting.staleness_flag or age_days > threshold_days:
        ratio = _POSTING_RECENCY_RATIO_STALE
    elif age_days > threshold_days / 2:
        ratio = _POSTING_RECENCY_RATIO_AGING
    elif age_days > threshold_days / 6:
        ratio = _POSTING_RECENCY_RATIO_RECENT
    else:
        ratio = _POSTING_RECENCY_RATIO_FRESH
    return {"raw": round(age_days, 1), "weight": weight, "contribution": round(weight * ratio)}


def _sponsorship_signal_bonus_component(posting: JobPosting | None) -> dict:
    fired = bool(posting and posting.sponsorship_signal)
    return {
        "raw": fired,
        "weight": _SPONSORSHIP_SIGNAL_BONUS_WEIGHT,
        "contribution": _SPONSORSHIP_SIGNAL_BONUS_WEIGHT if fired else 0,
    }


def _ai_profile_fit_component(application: JobApplication) -> dict:
    if application.match_analysis_json is None:
        # Never scored by the LLM yet -- match_score's column default
        # is 0, not NULL, so match_analysis_json (only ever set
        # together with a real match_score) is the actual "has this
        # been scored" signal, same distinction analytics_service.py
        # already relies on.
        return {"raw": None, "weight": _AI_PROFILE_FIT_WEIGHT, "contribution": 0}
    contribution = round(application.match_score / 100 * _AI_PROFILE_FIT_WEIGHT)
    return {"raw": application.match_score, "weight": _AI_PROFILE_FIT_WEIGHT, "contribution": contribution}


def compute_score_breakdown(
    application: JobApplication,
    settings: GlobalSettings | None = None,
    sponsorship_history_weight: float = _SPONSORSHIP_HISTORY_WEIGHT,
    wage_level_fit_weight: float = _WAGE_LEVEL_FIT_WEIGHT,
    active_keywords: list[str] | None = None,
    active_seniority_exclusions: list[str] | None = None,
) -> dict:
    """Pure function over an already-loaded application (posting +
    company relationships must be loaded/loadable) -- no db writes.
    `settings` now also drives posting_recency's staleness buckets (see
    _posting_recency_component) -- no longer unused.

    sponsorship_history_weight/wage_level_fit_weight default to this
    module's own shipped constants -- callers with a db session
    (recompute_score_breakdown) resolve the live adaptive value instead
    (see adaptation_service.py's b2 scoring_weights, config/
    adaptation.yaml). A caller with no db session gets byte-identical
    behavior to before b2, cold start included.

    active_keywords/active_seniority_exclusions default to None
    (role_title_match treats this as "not resolved," same neutral
    handling as a genuinely empty SearchKeyword table) rather than
    querying here -- keeps this function db-free; recompute_score_breakdown
    is the one caller with a session, so it resolves and passes them."""
    posting = application.posting
    company = posting.company if posting else None

    components = {
        "ai_profile_fit": _ai_profile_fit_component(application),
        "sponsorship_history": _sponsorship_history_component(company, sponsorship_history_weight),
        "wage_level_fit": _wage_level_fit_component(company, wage_level_fit_weight, posting),
        "role_title_match": _role_title_match_component(
            posting, active_keywords=active_keywords, active_seniority_exclusions=active_seniority_exclusions
        ),
        "worksite_clarity": _worksite_clarity_component(posting),
        "posting_recency": _posting_recency_component(posting, settings),
        "sponsorship_signal_bonus": _sponsorship_signal_bonus_component(posting),
    }
    total = min(100, sum(c["contribution"] for c in components.values()))
    return {"components": components, "total": total}


def recompute_score_breakdown(db: Session, application: JobApplication) -> dict:
    """Recomputes and persists score_breakdown from whatever data is
    currently on record (tier, wage level, signals, title/keywords, and
    the last LLM match_score if any) -- no LLM call. Safe to run on
    every application in bulk (see ingest/cli.py's rescore-all).

    Resolves sponsorship_history/wage_level_fit weights from
    adaptation_service (cold start = the config default, identical to
    this module's own hardcoded constant) -- a local import, matching
    this codebase's existing convention for avoiding a module-load-time
    circular import between the scoring and adaptation services.

    role_title_match's keyword/exclusion lists aren't part of that
    adaptive-weight system (there's no Tier 2 comparison type for them,
    unlike tier/wage_level -- see adaptation_service.py's
    _COMPARISON_WEIGHT_PARAMETER) -- they're just the candidate's
    current real SearchKeyword/SeniorityExclusion rows, queried fresh
    each call so a profile-driven retarget takes effect immediately."""
    from . import adaptation_service

    settings = get_or_create_settings(db)
    config = adaptation_service.get_config()
    sponsorship_history_weight = adaptation_service.get_current_value(
        db, "scoring_weight_sponsorship_history", config["scoring_weights"]["sponsorship_history"]["default"]
    )
    wage_level_fit_weight = adaptation_service.get_current_value(
        db, "scoring_weight_wage_level_fit", config["scoring_weights"]["wage_level_fit"]["default"]
    )
    active_keywords = [k.keyword for k in db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()]  # noqa: E712
    active_seniority_exclusions = [
        s.term for s in db.query(SeniorityExclusion).filter(SeniorityExclusion.is_active == True).all()  # noqa: E712
    ]
    breakdown = compute_score_breakdown(
        application, settings, sponsorship_history_weight, wage_level_fit_weight,
        active_keywords, active_seniority_exclusions,
    )
    application.score_breakdown = breakdown
    db.commit()
    return breakdown
