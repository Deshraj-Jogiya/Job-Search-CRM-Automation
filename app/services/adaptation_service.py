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

    t2 = require(data, "tier2", dict)
    require(t2, "min_samples_resume_variant", int, min=1)
    require(t2, "min_samples_source_comparison", int, min=1)
    require(t2, "min_samples_tier_comparison", int, min=1)
    require(t2, "min_replies_for_any_outcome_claim", int, min=1)
    require(t2, "weight_change_clamp_pct", int, min=1, max=100)

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
