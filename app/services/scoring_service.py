"""
Mechanical score components layered onto the existing LLM match score.
See WEIGHTING.md for the full rationale and the exact mapping from the
original spec's weights onto this codebase's real data -- short
version: this app's match_score has always been a single opaque
LLM judgment (0-100), never a set of named weighted sub-scores, so
"extending" it means folding that whole number in as one 30-point
component ("AI Profile Fit") alongside four new, purely mechanical
components computed from data intake/Phase 1 already collect --
Company.tier, Company.max_wage_level_15xx, JobPosting.worksite_ambiguous/
sponsorship_signal. Every component's raw value/weight/contribution is
recorded in score_breakdown so the UI can render exactly why an
application scored what it did (see application_detail.html /
queue.html's breakdown table).

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

from sqlalchemy.orm import Session

from ..models import Company, GlobalSettings, JobApplication, JobPosting, get_or_create_settings

_SPONSORSHIP_HISTORY_WEIGHT = 30
_WAGE_LEVEL_FIT_WEIGHT = 20
_WORKSITE_CLARITY_WEIGHT = 10
_SPONSORSHIP_SIGNAL_BONUS_WEIGHT = 10
_AI_PROFILE_FIT_WEIGHT = 30  # the folded-in original match_score, see module docstring

# Per-tier/per-level point values as RATIOS of their component's own
# weight, not fixed numbers -- b2's Tier 2 evidence can propose tuning
# _SPONSORSHIP_HISTORY_WEIGHT/_WAGE_LEVEL_FIT_WEIGHT within
# config/adaptation.yaml's scoring_weights bounds (see
# recompute_score_breakdown below); the ratios keep each tier's/level's
# relative standing intact when that happens. At the default weight
# (30/20) these ratios reproduce the exact original point values
# (30/18/8/0/4 and 20/15/8/3/6) -- see WEIGHTING.md.
_WAGE_LEVEL_FIT_RATIO = {"IV": 1.0, "III": 0.75, "II": 0.4, "I": 0.15}
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


def _wage_level_fit_component(company: Company | None, weight: float = _WAGE_LEVEL_FIT_WEIGHT) -> dict:
    level = company.max_wage_level_15xx if company else None
    ratio = _WAGE_LEVEL_FIT_RATIO.get(level, _WAGE_LEVEL_FIT_UNKNOWN_RATIO)
    return {"raw": level, "weight": weight, "contribution": round(weight * ratio)}


def _worksite_clarity_component(posting: JobPosting | None) -> dict:
    """No exact "is this a specific US metro" algorithm was specified --
    approximated here from JobPosting.location (a raw string from the
    source, not geocoded) plus the mechanical worksite_ambiguous flag
    (sponsorship_signals.py). A bare "Remote" with no city named scores
    the middle value, same as "no location given at all" -- both are
    real ambiguity, just not the explicit red-flag phrasing
    worksite_ambiguous catches. Documented as an approximation, not
    claimed as precise geolocation."""
    if posting is None:
        return {"raw": None, "weight": _WORKSITE_CLARITY_WEIGHT, "contribution": _WORKSITE_CLARITY_WEIGHT // 2}
    if posting.worksite_ambiguous:
        return {"raw": posting.location, "weight": _WORKSITE_CLARITY_WEIGHT, "contribution": 0}

    location = (posting.location or "").strip()
    if not location:
        contribution = 6
    elif "," in location:
        # A real "City, ST" (or similar) shape -- specific enough to
        # count as a named metro rather than a bare "Remote".
        contribution = 10
    elif "remote" in location.lower():
        contribution = 6
    else:
        contribution = 10
    return {"raw": location or None, "weight": _WORKSITE_CLARITY_WEIGHT, "contribution": contribution}


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
) -> dict:
    """Pure function over an already-loaded application (posting +
    company relationships must be loaded/loadable) -- no db writes.
    `settings` is accepted for future threshold-driven components but
    unused today (tier itself is precomputed on Company, not derived
    here) -- kept in the signature so callers don't need to change it
    later.

    sponsorship_history_weight/wage_level_fit_weight default to this
    module's own shipped constants -- callers with a db session
    (recompute_score_breakdown) resolve the live adaptive value instead
    (see adaptation_service.py's b2 scoring_weights, config/
    adaptation.yaml). A caller with no db session gets byte-identical
    behavior to before b2, cold start included."""
    posting = application.posting
    company = posting.company if posting else None

    components = {
        "ai_profile_fit": _ai_profile_fit_component(application),
        "sponsorship_history": _sponsorship_history_component(company, sponsorship_history_weight),
        "wage_level_fit": _wage_level_fit_component(company, wage_level_fit_weight),
        "worksite_clarity": _worksite_clarity_component(posting),
        "sponsorship_signal_bonus": _sponsorship_signal_bonus_component(posting),
    }
    total = min(100, sum(c["contribution"] for c in components.values()))
    return {"components": components, "total": total}


def recompute_score_breakdown(db: Session, application: JobApplication) -> dict:
    """Recomputes and persists score_breakdown from whatever data is
    currently on record (tier, wage level, signals, and the last LLM
    match_score if any) -- no LLM call. Safe to run on every
    application in bulk (see ingest/cli.py's rescore-all).

    Resolves sponsorship_history/wage_level_fit weights from
    adaptation_service (cold start = the config default, identical to
    this module's own hardcoded constant) -- a local import, matching
    this codebase's existing convention for avoiding a module-load-time
    circular import between the scoring and adaptation services."""
    from . import adaptation_service

    settings = get_or_create_settings(db)
    config = adaptation_service.get_config()
    sponsorship_history_weight = adaptation_service.get_current_value(
        db, "scoring_weight_sponsorship_history", config["scoring_weights"]["sponsorship_history"]["default"]
    )
    wage_level_fit_weight = adaptation_service.get_current_value(
        db, "scoring_weight_wage_level_fit", config["scoring_weights"]["wage_level_fit"]["default"]
    )
    breakdown = compute_score_breakdown(application, settings, sponsorship_history_weight, wage_level_fit_weight)
    application.score_breakdown = breakdown
    db.commit()
    return breakdown
