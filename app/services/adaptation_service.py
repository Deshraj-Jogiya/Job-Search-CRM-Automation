"""
Part B: the adaptive layer. Two tiers, deliberately NOT blurred:

Tier 1 (mechanical) -- self-tunes continuously, auto-applies, logs
every change. Qualifies only when feedback is deterministic, arrives
within one run, and ground truth is unambiguous (a corrected dedupe
decision, a user-flagged wrong sponsorship signal). See the
tune_*/record_* functions below.

Tier 2 (strategy) -- outcome-dependent, slow, sparse, confounded.
NEVER auto-applies. evaluate_comparison() is the one generic engine
behind every Tier 2 comparison the spec names (resume variant, source,
tier, wage level) -- same evidence gates, same confound check, same
NOT_REPORTABLE/REPORTABLE/APPLIED state machine, applied to whichever
segmentation function from metrics_service.py it's pointed at, rather
than four separate copies of the same logic.

Every change either tier makes writes an AdaptationLog row (see
models.py) -- what changed, old/new value, the evidence, the sample
size, tier, auto vs approved. AdaptiveParameterValue holds the CURRENT
effective value of anything Tier 1 self-tunes or a Tier 2 proposal
adjusted; a parameter with no row there is still at its config default
-- cold start (b3.4) needs no special-case code, it falls out of that
directly.
"""

import math
from datetime import timedelta
from difflib import SequenceMatcher

from sqlalchemy.orm import Session

from ..config_loader import HotReloadableYaml, require
from ..database import utcnow
from ..models import AdaptationLog, AdaptiveParameterValue
from . import metrics_service
from .activity_logger import log_activity


def _validate(data: dict) -> None:
    t1 = require(data, "tier1", dict)
    dt = require(t1, "dedupe_threshold", dict)
    require(dt, "min", float, min=0.0, max=1.0)
    require(dt, "max", float, min=0.0, max=1.0)
    require(dt, "default", float, min=0.0, max=1.0)
    require(t1, "column_mapping_fuzzy_threshold", float, min=0.0, max=1.0)

    t2 = require(data, "tier2", dict)
    require(t2, "min_samples_resume_variant", int, min=1)
    require(t2, "min_samples_source_comparison", int, min=1)
    require(t2, "min_samples_tier_comparison", int, min=1)
    require(t2, "min_samples_wage_level_comparison", int, min=1)
    require(t2, "min_replies_for_any_outcome_claim", int, min=1)
    require(t2, "weight_change_clamp_pct", int, min=1, max=100)
    require(t2, "confound_share_diff_pct", int, min=1, max=100)

    sw = require(data, "scoring_weights", dict)
    for key in ("sponsorship_history", "wage_level_fit"):
        entry = require(sw, key, dict)
        require(entry, "min", int, min=0)
        require(entry, "max", int, min=0)
        require(entry, "default", int, min=0)

    g = require(data, "guardrails", dict)
    require(g, "exploration_pct", int, min=0, max=100)


_STORE = HotReloadableYaml("adaptation.yaml", validate_fn=_validate)


def get_config() -> dict:
    return _STORE.get()


# ---------------------------------------------------------------------------
# Shared audit trail (b3.3)
# ---------------------------------------------------------------------------

def log_adaptation(
    db: Session,
    tier: str,
    subsystem: str,
    parameter: str,
    old_value,
    new_value,
    triggering_evidence: dict | None = None,
    sample_size: int | None = None,
    status: str = "applied",
) -> AdaptationLog:
    entry = AdaptationLog(
        tier=tier, subsystem=subsystem, parameter=parameter,
        old_value=old_value, new_value=new_value,
        triggering_evidence=triggering_evidence, sample_size=sample_size, status=status,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    log_activity(
        db,
        f"Adaptation [{tier}/{subsystem}] {parameter}: {old_value!r} -> {new_value!r} ({status}).",
        "INFO",
    )
    return entry


class AdaptationServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def revert_adaptation(db: Session, log_id: int) -> AdaptationLog:
    """Restores a parameter to its old_value and marks the ORIGINAL row
    reverted -- writes a NEW log row for the restoration itself so the
    audit trail stays append-only (never edits/deletes history)."""
    entry = db.query(AdaptationLog).filter(AdaptationLog.id == log_id).first()
    if not entry:
        raise AdaptationServiceError(f"Adaptation log entry {log_id} not found.")
    if entry.status == "reverted":
        raise AdaptationServiceError("Already reverted.")
    if entry.old_value is None:
        raise AdaptationServiceError("Nothing to revert to -- this entry has no recorded old_value.")

    set_current_value(db, entry.parameter, entry.old_value)
    entry.status = "reverted"
    entry.reverted_at = utcnow()
    db.commit()

    log_adaptation(
        db, entry.tier, entry.subsystem, entry.parameter,
        old_value=entry.new_value, new_value=entry.old_value,
        triggering_evidence={"reverted_log_id": log_id}, status="reverted",
    )
    return entry


def revert_all_since(db: Session, since) -> list[AdaptationLog]:
    """b3.3's bulk revert -- everything not already reverted, applied
    or approved (proposed/rejected rows never touched anything to
    revert), newest first so a chain of dependent changes unwinds in
    the right order."""
    entries = (
        db.query(AdaptationLog)
        .filter(
            AdaptationLog.created_at >= since,
            AdaptationLog.status.in_(("applied", "approved")),
        )
        .order_by(AdaptationLog.created_at.desc())
        .all()
    )
    reverted = []
    for entry in entries:
        reverted.append(revert_adaptation(db, entry.id))
    return reverted


# ---------------------------------------------------------------------------
# Current effective values (cold start = config default, b3.4)
# ---------------------------------------------------------------------------

def get_current_value(db: Session, parameter: str, default: float) -> float:
    row = db.query(AdaptiveParameterValue).filter(AdaptiveParameterValue.parameter == parameter).first()
    return row.value if row else default


def set_current_value(db: Session, parameter: str, value: float) -> None:
    row = db.query(AdaptiveParameterValue).filter(AdaptiveParameterValue.parameter == parameter).first()
    if row:
        row.value = value
        row.updated_at = utcnow()
    else:
        db.add(AdaptiveParameterValue(parameter=parameter, value=value))
    db.commit()


# ---------------------------------------------------------------------------
# B3.2 -- clamp any proposed weight/threshold change
# ---------------------------------------------------------------------------

def clamp_weight_change(current: float, proposed: float, clamp_pct: int, floor: float, ceiling: float) -> float:
    """Any single approval can move a value by at most clamp_pct of its
    CURRENT value, and never outside [floor, ceiling] regardless --
    both bounds enforced together, the tighter one wins."""
    max_delta = abs(current) * (clamp_pct / 100)
    clamped = max(current - max_delta, min(current + max_delta, proposed))
    return max(floor, min(ceiling, clamped))


# ---------------------------------------------------------------------------
# B1.4 -- fuzzy dedupe threshold, self-tuned within config bounds
# ---------------------------------------------------------------------------

_DEDUPE_PARAMETER = "dedupe_threshold"


def current_dedupe_threshold(db: Session, config: dict | None = None) -> float:
    config = config or get_config()
    return get_current_value(db, _DEDUPE_PARAMETER, config["tier1"]["dedupe_threshold"]["default"])


def fuzzy_title_match(title_a: str, title_b: str, threshold: float) -> bool:
    """stdlib difflib ratio (no new dependency) -- deliberately a
    plain string-similarity score, not a semantic one: two genuinely
    different roles that happen to share most of their words (e.g.
    "Data Engineer" vs "Senior Data Engineer") sit close together on
    this scale on purpose, which is exactly the class of near-duplicate
    this threshold exists to catch. is_repost detection (recency gap)
    still runs on TOP of a fuzzy match, unchanged -- this only widens
    what counts as "the same title," it doesn't touch the repost-vs-
    still-live decision."""
    if not title_a or not title_b:
        return False
    return SequenceMatcher(None, title_a.lower(), title_b.lower()).ratio() >= threshold


def record_dedupe_correction(db: Session, was_false_merge: bool, config: dict | None = None) -> float:
    """A false merge (two genuinely different postings got treated as
    the same one) means the threshold was too LOOSE -- raise it. A
    false split (the same posting got treated as two) means it was too
    TIGHT -- lower it. Nudges by a fixed small step, clamped to the
    config-declared [min, max], every change logged."""
    config = config or get_config()
    bounds = config["tier1"]["dedupe_threshold"]
    current = current_dedupe_threshold(db, config)
    step = 0.01
    proposed = current + step if was_false_merge else current - step
    new_value = max(bounds["min"], min(bounds["max"], proposed))

    if new_value != current:
        set_current_value(db, _DEDUPE_PARAMETER, new_value)
        log_adaptation(
            db, "tier1", "dedupe_threshold", _DEDUPE_PARAMETER, current, new_value,
            triggering_evidence={"was_false_merge": was_false_merge}, status="applied",
        )
    return new_value


# ---------------------------------------------------------------------------
# B1.3 -- schema header-mapping learning (USCIS/DOL LCA/OEWS loaders)
# ---------------------------------------------------------------------------

_COLUMN_MAPPING_SUBSYSTEM = "column_mapping"


def current_column_mapping_fuzzy_threshold(db: Session, config: dict | None = None) -> float:
    config = config or get_config()
    return get_current_value(
        db, "column_mapping_fuzzy_threshold", config["tier1"]["column_mapping_fuzzy_threshold"]
    )


def record_column_mapping_correction(db: Session, source: str, field: str, wrong_guess, correct_column: str) -> AdaptationLog:
    """A user edited data/column_mappings.yaml (see
    app/ingest/column_utils.py) to correct a fuzzy-guessed header --
    logged so future fuzzy matches for this same (source, field) weight
    toward the now-confirmed real column string (see
    confirmed_column_names). wrong_guess may be None the first time a
    field is ever set manually with no prior guess on record."""
    return log_adaptation(
        db, "tier1", _COLUMN_MAPPING_SUBSYSTEM, f"{source}:{field}", wrong_guess, correct_column,
        triggering_evidence={"source": source, "field": field}, status="applied",
    )


def confirmed_column_names(db: Session, source: str, field: str) -> list[str]:
    """Every real column string ever confirmed correct for this
    (source, field) pair -- extra fuzzy-match candidates for a brand
    new file whose exact header text this app hasn't seen before, on
    top of the loader's own hardcoded alias list."""
    rows = (
        db.query(AdaptationLog)
        .filter(
            AdaptationLog.subsystem == _COLUMN_MAPPING_SUBSYSTEM,
            AdaptationLog.parameter == f"{source}:{field}",
            AdaptationLog.status == "applied",
        )
        .all()
    )
    return sorted({row.new_value for row in rows if row.new_value})


# ---------------------------------------------------------------------------
# B1.2 -- ATS slug guessing strategy (reordered by observed hit rate)
# ---------------------------------------------------------------------------

_SLUG_STRATEGY_SUBSYSTEM = "ats_slug_strategy"


def record_slug_strategy_hit(db: Session, ats_type: str, form: str) -> AdaptationLog:
    """A discover_slugs() probe just confirmed ats_type's real board
    slug came from candidate form `form` ("no_space"/"hyphenated").
    Logged as an 'applied' Tier 1 event -- this only ever reorders
    which guess is tried FIRST next time, it never removes a guess or
    stops trying the other form, so it's safe to auto-apply
    continuously like b1.4's threshold."""
    return log_adaptation(
        db, "tier1", _SLUG_STRATEGY_SUBSYSTEM, f"{ats_type}:{form}", None, None,
        triggering_evidence={"ats_type": ats_type, "form": form}, status="applied",
    )


def recommend_slug_form_order(db: Session) -> dict:
    """{ats_type: [form, ...]} ordered by observed hit count, most
    successful form first -- feeds discover_slugs's slug_form_order
    param. Cold start (no hits recorded yet) returns {}, which makes
    discover_slugs try its own fixed default order -- byte-identical
    to pre-b1.2 behavior (b3.4)."""
    rows = (
        db.query(AdaptationLog)
        .filter(AdaptationLog.subsystem == _SLUG_STRATEGY_SUBSYSTEM, AdaptationLog.status == "applied")
        .all()
    )
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        ats_type, _, form = row.parameter.partition(":")
        counts.setdefault(ats_type, {})
        counts[ats_type][form] = counts[ats_type].get(form, 0) + 1
    return {
        ats_type: sorted(forms_counts, key=lambda form: -forms_counts[form])
        for ats_type, forms_counts in counts.items()
    }


# ---------------------------------------------------------------------------
# B1.5 -- sponsorship regex misfires (accumulated, never auto-applied)
# ---------------------------------------------------------------------------

_SPONSORSHIP_MISFIRE_SUBSYSTEM = "sponsorship_regex"


def record_sponsorship_misfire(db: Session, posting_id: int, label: str) -> AdaptationLog:
    """One-click 'this flag was wrong' on a sponsorship-blocked posting.
    Unlike b1.4's threshold, a regex PATTERN isn't a bounded float --
    narrowing it is a code change, so this can only ever accumulate
    evidence and surface it (status='proposed'), never write a new
    current_value or flip status to 'applied' by itself."""
    return log_adaptation(
        db, "tier1", _SPONSORSHIP_MISFIRE_SUBSYSTEM, label, None, None,
        triggering_evidence={"posting_id": posting_id}, status="proposed",
    )


def sponsorship_misfire_report(db: Session, min_misfires: int = 3) -> list[dict]:
    """Groups accumulated corrections by pattern label -- the 'surface
    responsible pattern' half of b1.5. Only labels at or above
    min_misfires are returned; one stray correction shouldn't flag a
    pattern as broken. Worst-offender first."""
    rows = (
        db.query(AdaptationLog)
        .filter(
            AdaptationLog.subsystem == _SPONSORSHIP_MISFIRE_SUBSYSTEM,
            AdaptationLog.status == "proposed",
        )
        .all()
    )
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.parameter] = counts.get(row.parameter, 0) + 1
    return sorted(
        ({"label": label, "misfire_count": count} for label, count in counts.items() if count >= min_misfires),
        key=lambda r: -r["misfire_count"],
    )


# ---------------------------------------------------------------------------
# B1.8 -- queue supply visibility (plain counts, no target)
# ---------------------------------------------------------------------------

def queue_supply_report(db: Session) -> dict:
    """New qualifying postings entering the queue after dedup/
    filtering, by tier/lane, plus a 7-day rolling average --
    informational only, no target rendered anywhere (see
    queue_service.py's own no-quota guardrail)."""
    from ..models import Company, JobApplication, JobPosting  # local import avoids a circular import at module load

    since_7d = utcnow() - timedelta(days=7)
    today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    def _count(start, tier_filter=None):
        query = (
            db.query(JobApplication)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .outerjoin(Company, JobPosting.company_id == Company.id)
            .filter(JobPosting.created_at >= start, JobPosting.sponsorship_blocked.is_(False))
        )
        if tier_filter == "cap_exempt":
            query = query.filter(Company.is_cap_exempt.is_(True))
        elif tier_filter == "tier_ab":
            query = query.filter(Company.is_cap_exempt.isnot(True), Company.tier.in_(("A", "B")))
        elif tier_filter == "other":
            query = query.filter(
                Company.is_cap_exempt.isnot(True),
                (Company.tier.is_(None)) | (~Company.tier.in_(("A", "B"))),
            )
        return query.count()

    today_total = _count(today_start)
    rolling_total = _count(since_7d)
    return {
        "today": today_total,
        "rolling_7d_avg": round(rolling_total / 7, 1),
        "today_by_lane": {
            "cap_exempt": _count(today_start, "cap_exempt"),
            "tier_ab": _count(today_start, "tier_ab"),
            "other": _count(today_start, "other"),
        },
    }


# ---------------------------------------------------------------------------
# B1.6 -- source yield (computed from existing data, no new tracking table)
# ---------------------------------------------------------------------------

def source_yield_report(db: Session) -> list[dict]:
    """ingested -> surviving filters -> surfaced in queue -> applied,
    per source. A recommendation only -- see recommend_source_reorder;
    this NEVER auto-disables a source (b1.6's own explicit rule)."""
    from ..models import JobApplication, JobPosting

    sources = [s for (s,) in db.query(JobPosting.source).distinct().all()]
    report = []
    for source in sources:
        ingested = db.query(JobPosting).filter(JobPosting.source == source).count()
        surviving = (
            db.query(JobPosting)
            .filter(JobPosting.source == source, JobPosting.sponsorship_blocked.is_(False))
            .count()
        )
        surfaced = (
            db.query(JobApplication)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .filter(
                JobPosting.source == source, JobPosting.sponsorship_blocked.is_(False),
                JobApplication.skipped_at.is_(None),
                JobApplication.status.notin_(("Applied", "Rejected", "Interviewing", "Offer", "Not Selected")),
            )
            .count()
        )
        applied = (
            db.query(JobApplication)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .filter(JobPosting.source == source, JobApplication.applied_at.isnot(None))
            .count()
        )
        report.append({
            "source": source, "ingested": ingested, "surviving": surviving,
            "surfaced": surfaced, "applied": applied,
            "yield_pct": round(100 * applied / ingested, 1) if ingested else None,
        })
    report.sort(key=lambda r: r["yield_pct"] or 0, reverse=True)
    return report


def recommend_source_reorder(db: Session, min_ingested: int = 10) -> list[str]:
    """Sources ordered best-yield-first, for intake_service.py's
    polling loop to poll in that order -- a RECOMMENDATION applied
    directly to poll ORDER (harmless to reorder), never to whether a
    source runs at all. Sources under min_ingested postings are left
    in their original relative position -- too little data to rank."""
    report = source_yield_report(db)
    ranked = [r["source"] for r in report if r["ingested"] >= min_ingested]
    unranked = [r["source"] for r in report if r["ingested"] < min_ingested]
    return ranked + unranked


# ---------------------------------------------------------------------------
# B1.7 -- posting staleness (computed from existing data)
# ---------------------------------------------------------------------------

def typical_time_to_close_days(db: Session) -> dict:
    """Median (last_seen_at - first_seen_at) per source, in days -- the
    real proxy for "how long does a posting on this platform typically
    stay live" this schema actually has (there's no explicit "closed"
    event anywhere). Warn-only input for staleness_flag, never a filter."""
    from ..models import JobPosting

    sources = [s for (s,) in db.query(JobPosting.source).distinct().all()]
    result = {}
    for source in sources:
        rows = (
            db.query(JobPosting.first_seen_at, JobPosting.last_seen_at)
            .filter(JobPosting.source == source)
            .all()
        )
        days = sorted((last - first).days for first, last in rows if last and first)
        if not days:
            continue
        n = len(days)
        median = days[n // 2] if n % 2 else (days[n // 2 - 1] + days[n // 2]) / 2
        result[source] = {"median_days": median, "sample_size": n}
    return result


# ---------------------------------------------------------------------------
# B2 -- Tier 2 evidence engine. One generic comparison, NEVER auto-applies.
# ---------------------------------------------------------------------------

_COMPARISON_SEGMENTATION_FNS = {
    "resume_variant": metrics_service.by_resume_version,
    "source": metrics_service.by_source,
    "tier": metrics_service.by_company_tier,
    "wage_level": metrics_service.by_wage_level,
}

_COMPARISON_SAMPLE_KEYS = {
    "resume_variant": "min_samples_resume_variant",
    "source": "min_samples_source_comparison",
    "tier": "min_samples_tier_comparison",
    "wage_level": "min_samples_wage_level_comparison",
}

# Only comparisons with a natural, ordered "which segment SHOULD win"
# get a proposed numeric weight adjustment -- resume_variant/source
# have no such ordering (no honest single knob to auto-propose), so
# they stay informational-only REPORTABLE results, never a proposal.
_COMPARISON_WEIGHT_PARAMETER = {
    "tier": ("scoring_weight_sponsorship_history", "sponsorship_history"),
    "wage_level": ("scoring_weight_wage_level_fit", "wage_level_fit"),
}
_EXPECTED_RANK = {
    "tier": {"A": 4, "B": 3, "C": 2, "X": 1, "unknown": 0},
    "wage_level": {"IV": 4, "III": 3, "II": 2, "I": 1, "unknown": 0},
}


def _segment_application_ids(db: Session, comparison_type: str, segment_value: str) -> list[int]:
    """Applied-application ids belonging to one segment value of one
    comparison type -- shared by the confound check below. Local
    imports avoid a circular import at module load (same convention as
    queue_supply_report/source_yield_report above)."""
    from ..models import Company, JobApplication, JobPosting, ProfileVariant

    query = (
        db.query(JobApplication.id)
        .join(JobPosting, JobApplication.posting_id == JobPosting.id)
        .filter(JobApplication.applied_at.isnot(None))
    )
    if comparison_type == "resume_variant":
        if segment_value == "(unassigned)":
            query = query.filter(JobApplication.profile_variant_id.is_(None))
        else:
            variant = db.query(ProfileVariant).filter(ProfileVariant.name == segment_value).first()
            if not variant:
                return []
            query = query.filter(JobApplication.profile_variant_id == variant.id)
    elif comparison_type == "source":
        query = query.filter(JobPosting.source == segment_value)
    elif comparison_type == "tier":
        query = query.join(Company, JobPosting.company_id == Company.id).filter(
            Company.tier == (None if segment_value == "unknown" else segment_value)
        )
    elif comparison_type == "wage_level":
        query = query.join(Company, JobPosting.company_id == Company.id).filter(
            Company.max_wage_level_15xx == (None if segment_value == "unknown" else segment_value)
        )
    return [row_id for (row_id,) in query.all()]


def _dimension_share(db: Session, application_ids: list[int], dimension: str) -> dict:
    """{value: proportion} of `dimension` ("source" or "tier") among
    the given applied applications -- the confound check's raw input."""
    if not application_ids:
        return {}
    from ..models import Company, JobApplication, JobPosting

    rows = (
        db.query(JobPosting.source, Company.tier)
        .join(JobApplication, JobApplication.posting_id == JobPosting.id)
        .outerjoin(Company, JobPosting.company_id == Company.id)
        .filter(JobApplication.id.in_(application_ids))
        .all()
    )
    counts: dict[str, int] = {}
    for source, tier in rows:
        key = source if dimension == "source" else (tier or "unknown")
        counts[key] = counts.get(key, 0) + 1
    total = len(rows)
    return {k: v / total for k, v in counts.items()}


def _is_confounded(db: Session, comparison_type: str, seg_a: str, seg_b: str, threshold_pct: int) -> bool:
    """b3.5: True when the two compared segments differ in their
    company-tier (or, for a tier comparison itself, source) composition
    by more than threshold_pct for any value -- the reply-rate
    difference could just be that, not the thing actually being
    compared. Checking tier-as-confound of a tier comparison would be
    circular, so a tier comparison checks source composition instead."""
    dimension = "source" if comparison_type == "tier" else "tier"
    share_a = _dimension_share(db, _segment_application_ids(db, comparison_type, seg_a), dimension)
    share_b = _dimension_share(db, _segment_application_ids(db, comparison_type, seg_b), dimension)
    for key in set(share_a) | set(share_b):
        if abs(share_a.get(key, 0.0) - share_b.get(key, 0.0)) * 100 > threshold_pct:
            return True
    return False


def _proportion_ci_95(p1: float, n1: int, p2: float, n2: int) -> dict:
    """Wald 95% CI for the difference p1 - p2 of two proportions (each
    0.0-1.0). Plain stdlib math, no scipy -- adequate at the sample
    sizes this evidence gate already requires (n >= 30-50 per the
    config-declared min_samples_*)."""
    diff = p1 - p2
    se = math.sqrt(p1 * (1 - p1) / n1 + p2 * (1 - p2) / n2) if n1 and n2 else 0.0
    margin = 1.96 * se
    return {"diff": round(diff, 4), "low": round(diff - margin, 4), "high": round(diff + margin, 4)}


def evaluate_comparison(db: Session, comparison_type: str, config: dict | None = None) -> dict:
    """The one generic Tier 2 engine behind every named comparison
    (resume_variant/source/tier/wage_level) -- same evidence gates,
    same confound check, same state machine, pointed at whichever
    metrics_service.py segmentation function matches. NEVER writes
    anything; a pure read returning the current readiness state.

    States:
      NOT_REPORTABLE -- raw segment numbers only, no best/worst, no CI,
        no proposal. Below the sample-size or reply-count gate.
      CONFOUNDED -- crossed the evidence gate, but the two leading
        segments differ too much on another tracked dimension (b3.5) --
        proposal withheld even though the numbers alone would qualify.
      REPORTABLE -- best/worst segment, a 95% CI on the reply-rate
        difference, and (only for tier/wage_level, only when the CI
        excludes zero) a proposed_adjustment -- approve_comparison_proposal
        applies it, reject_comparison_proposal just logs the decision."""
    if comparison_type not in _COMPARISON_SEGMENTATION_FNS:
        raise AdaptationServiceError(f"Unknown comparison_type '{comparison_type}'.")
    config = config or get_config()
    segments = _COMPARISON_SEGMENTATION_FNS[comparison_type](db)
    total_sent = sum(s["sent"] for s in segments)
    total_replied = sum(s["replied"] for s in segments)
    min_samples = config["tier2"][_COMPARISON_SAMPLE_KEYS[comparison_type]]
    min_replies = config["tier2"]["min_replies_for_any_outcome_claim"]

    result = {
        "comparison_type": comparison_type,
        "segments": segments,
        "total_sent": total_sent,
        "total_replied": total_replied,
        "min_samples_required": min_samples,
        "min_replies_required": min_replies,
        "state": "NOT_REPORTABLE",
    }

    eligible = [s for s in segments if s["sent"] >= 1]
    if total_sent < min_samples or total_replied < min_replies or len(eligible) < 2:
        return result

    ranked = sorted(eligible, key=lambda s: s["reply_rate"] or 0, reverse=True)
    best, worst = ranked[0], ranked[-1]
    if best["segment"] == worst["segment"]:
        return result  # only one real segment with data -- nothing to compare

    if _is_confounded(db, comparison_type, best["segment"], worst["segment"], config["tier2"]["confound_share_diff_pct"]):
        result["state"] = "CONFOUNDED"
        result["confounded_segments"] = [best["segment"], worst["segment"]]
        return result

    ci = _proportion_ci_95(
        (best["reply_rate"] or 0) / 100, best["sent"],
        (worst["reply_rate"] or 0) / 100, worst["sent"],
    )
    result["state"] = "REPORTABLE"
    result["best_segment"] = best["segment"]
    result["worst_segment"] = worst["segment"]
    result["confidence_interval"] = ci
    result["proposed_adjustment"] = None

    if comparison_type in _COMPARISON_WEIGHT_PARAMETER and ci["low"] > 0:
        parameter, weight_key = _COMPARISON_WEIGHT_PARAMETER[comparison_type]
        bounds = config["scoring_weights"][weight_key]
        current = get_current_value(db, parameter, bounds["default"])
        expected_rank = _EXPECTED_RANK[comparison_type]
        # Evidence CONFIRMS the assumed ranking (e.g. tier A really does
        # outreply tier C) -> nudge the weight up, more confidence in
        # the signal. Evidence CONTRADICTS it (a lower tier winning) ->
        # nudge down, the signal is less trustworthy than assumed.
        confirms_expected_order = expected_rank.get(best["segment"], 0) >= expected_rank.get(worst["segment"], 0)
        nudge_factor = 1.1 if confirms_expected_order else 0.9
        proposed_value = clamp_weight_change(
            current, current * nudge_factor, config["tier2"]["weight_change_clamp_pct"], bounds["min"], bounds["max"],
        )
        if proposed_value != current:
            result["proposed_adjustment"] = {
                "parameter": parameter,
                "current_value": current,
                "proposed_value": proposed_value,
                "confirms_expected_order": confirms_expected_order,
                "reason": (
                    f"'{best['segment']}' reply rate significantly {'exceeds' if confirms_expected_order else 'trails'} "
                    f"the assumed ranking against '{worst['segment']}'."
                ),
            }

    return result


def approve_comparison_proposal(db: Session, comparison_type: str, config: dict | None = None) -> AdaptationLog:
    """Re-evaluates fresh (evidence may have moved since the view was
    rendered) and, only if still REPORTABLE with a live proposal,
    applies it -- writes the new AdaptiveParameterValue and an
    'applied' AdaptationLog row citing the evidence, one-click
    revertable via the existing revert_adaptation."""
    result = evaluate_comparison(db, comparison_type, config)
    proposal = result.get("proposed_adjustment")
    if result["state"] != "REPORTABLE" or not proposal:
        raise AdaptationServiceError(f"No approvable proposal for '{comparison_type}' right now (state={result['state']}).")

    set_current_value(db, proposal["parameter"], proposal["proposed_value"])
    return log_adaptation(
        db, "tier2", f"comparison:{comparison_type}", proposal["parameter"],
        proposal["current_value"], proposal["proposed_value"],
        triggering_evidence={
            "best_segment": result["best_segment"], "worst_segment": result["worst_segment"],
            "confidence_interval": result["confidence_interval"],
        },
        sample_size=result["total_sent"], status="applied",
    )


def reject_comparison_proposal(db: Session, comparison_type: str, config: dict | None = None) -> AdaptationLog:
    """Logs the decision for the audit trail -- no current_value
    change. A rejected comparison isn't suppressed from view; if the
    same proposal recurs on later evidence, evaluate_comparison shows
    it again and the user can reject (or approve) it again."""
    result = evaluate_comparison(db, comparison_type, config)
    proposal = result.get("proposed_adjustment")
    return log_adaptation(
        db, "tier2", f"comparison:{comparison_type}",
        proposal["parameter"] if proposal else comparison_type,
        proposal["current_value"] if proposal else None,
        proposal["proposed_value"] if proposal else None,
        triggering_evidence={"best_segment": result.get("best_segment"), "worst_segment": result.get("worst_segment")},
        sample_size=result["total_sent"], status="rejected",
    )
