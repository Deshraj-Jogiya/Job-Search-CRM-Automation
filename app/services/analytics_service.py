"""
Phase 7: outcome analytics. Every number here is computed directly
from data this app already collects -- no estimates, no synthetic
"expected" values. Two honesty limits worth knowing before trusting
these numbers:

1. There's no inbox-scanning integration in this build, so anything
   past "Applied" (interviewing/offer/not-selected) is only as
   complete as the user's own Mark as Interviewing/Offer/Not Selected
   clicks (confirmation_service.py) -- an application that genuinely
   got an interview but was never marked will show up as still
   "Applied" here, understating the real interview rate, never
   overstating it.
2. Status is a single current value, not a history -- "how many
   applications are currently Approved" is a real, exact snapshot;
   it is not the same as "how many ever passed through Approved."
   Where a rate needs to know whether a stage was ever reached
   (interviewing, specifically), this module uses the dedicated
   interviewing_at/offer_at/not_selected_at timestamps rather than
   current status, precisely so an application that went straight
   from Applied to Offer doesn't silently vanish from that count.

Read-only -- this module only ever queries, never mutates.
"""

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..models import Company, JobApplication, JobPosting

_FUNNEL_ORDER = [
    "Ingested", "Tailored", "Pending Confirmation", "Needs Review",
    "Approved", "Applied", "Interviewing", "Offer", "Not Selected", "Rejected",
]

_SCORE_BANDS = [("0-59", 0, 59), ("60-74", 60, 74), ("75-89", 75, 89), ("90-100", 90, 100)]


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(100 * numerator / denominator, 1)


def status_funnel(db: Session) -> list[dict]:
    counts = dict(
        db.query(JobApplication.status, func.count(JobApplication.id)).group_by(JobApplication.status).all()
    )
    total = sum(counts.values())
    ordered = [s for s in _FUNNEL_ORDER if s in counts]
    ordered += [s for s in counts if s not in _FUNNEL_ORDER]  # don't silently drop an unrecognized status
    return [{"status": s, "count": counts[s], "pct_of_total": _rate(counts[s], total)} for s in ordered]


def conversion_rates(db: Session) -> dict:
    total = db.query(func.count(JobApplication.id)).scalar() or 0
    applied = db.query(func.count(JobApplication.id)).filter(JobApplication.applied_at.isnot(None)).scalar() or 0
    interviewed = (
        db.query(func.count(JobApplication.id)).filter(JobApplication.interviewing_at.isnot(None)).scalar() or 0
    )
    offers = db.query(func.count(JobApplication.id)).filter(JobApplication.offer_at.isnot(None)).scalar() or 0
    not_selected = (
        db.query(func.count(JobApplication.id)).filter(JobApplication.not_selected_at.isnot(None)).scalar() or 0
    )
    return {
        "total_postings": total,
        "applied": applied,
        "interviewed": interviewed,
        "offers": offers,
        "not_selected": not_selected,
        "apply_rate": _rate(applied, total),
        "interview_rate_of_applied": _rate(interviewed, applied),
        "offer_rate_of_applied": _rate(offers, applied),
    }


def speed_to_apply(db: Session) -> dict:
    """Directly validates (or doesn't) the core 'early applicant' value
    prop from CLAUDE.md -- how many hours/days typically pass between a
    posting first appearing and this app actually applying to it."""
    rows = (
        db.query(JobPosting.first_seen_at, JobApplication.applied_at)
        .join(JobApplication, JobApplication.posting_id == JobPosting.id)
        .filter(JobApplication.applied_at.isnot(None))
        .all()
    )
    if not rows:
        return {"count": 0, "median_hours": None, "avg_hours": None}
    hours = sorted((applied - first_seen).total_seconds() / 3600 for first_seen, applied in rows)
    n = len(hours)
    median = hours[n // 2] if n % 2 else (hours[n // 2 - 1] + hours[n // 2]) / 2
    return {"count": n, "median_hours": round(median, 1), "avg_hours": round(sum(hours) / n, 1)}


def by_source(db: Session) -> list[dict]:
    results = []
    sources = db.query(JobPosting.source).join(JobApplication, JobApplication.posting_id == JobPosting.id).distinct().all()
    for (source,) in sources:
        total = (
            db.query(func.count(JobApplication.id))
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .filter(JobPosting.source == source)
            .scalar()
        ) or 0
        applied = (
            db.query(func.count(JobApplication.id))
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .filter(JobPosting.source == source, JobApplication.applied_at.isnot(None))
            .scalar()
        ) or 0
        # match_score defaults to 0 (not NULL) until an application is
        # actually scored -- match_analysis_json is the real "has this
        # been scored" signal, set together with match_score by
        # matching_service.score_application(). Filtering on
        # match_score itself would silently mix "never scored" in with
        # genuine low scores, dragging every average toward 0.
        avg_score = (
            db.query(func.avg(JobApplication.match_score))
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .filter(JobPosting.source == source, JobApplication.match_analysis_json.isnot(None))
            .scalar()
        )
        results.append(
            {
                "source": source,
                "total": total,
                "applied": applied,
                "apply_rate": _rate(applied, total),
                "avg_match_score": round(avg_score, 1) if avg_score is not None else None,
            }
        )
    results.sort(key=lambda r: r["total"], reverse=True)
    return results


def by_score_band(db: Session) -> list[dict]:
    """Restricted to applications that have actually been scored
    (match_analysis_json set) -- otherwise a never-scored application
    (match_score's column default is 0, not NULL) would land in the
    0-59 band and read as a genuine low match rather than "not
    evaluated yet."."""
    results = []
    for label, low, high in _SCORE_BANDS:
        total = (
            db.query(func.count(JobApplication.id))
            .filter(
                JobApplication.match_score >= low,
                JobApplication.match_score <= high,
                JobApplication.match_analysis_json.isnot(None),
            )
            .scalar()
        ) or 0
        applied = (
            db.query(func.count(JobApplication.id))
            .filter(
                JobApplication.match_score >= low,
                JobApplication.match_score <= high,
                JobApplication.match_analysis_json.isnot(None),
                JobApplication.applied_at.isnot(None),
            )
            .scalar()
        ) or 0
        results.append({"band": label, "total": total, "applied": applied, "apply_rate": _rate(applied, total)})
    return results


def flag_correlation(db: Session) -> dict:
    """Do scam/repost/stale-flagged postings convert worse than clean
    ones? Flags are informational-only elsewhere in this app (never
    auto-filtered, per CLAUDE.md) -- this just shows whether that
    caution has actually been earning its keep."""

    def _counts(flagged: bool):
        query = db.query(func.count(JobApplication.id)).join(JobPosting, JobApplication.posting_id == JobPosting.id)
        condition = (JobPosting.scam_flag_reason.isnot(None)) | (JobPosting.staleness_flag == True) | (JobPosting.repost_count > 0)  # noqa: E712
        query = query.filter(condition if flagged else ~condition)
        total = query.scalar() or 0
        applied = query.filter(JobApplication.applied_at.isnot(None)).scalar() or 0
        return total, applied

    flagged_total, flagged_applied = _counts(True)
    clean_total, clean_applied = _counts(False)
    return {
        "flagged": {"total": flagged_total, "applied": flagged_applied, "apply_rate": _rate(flagged_applied, flagged_total)},
        "clean": {"total": clean_total, "applied": clean_applied, "apply_rate": _rate(clean_applied, clean_total)},
    }


def company_memory_summary(db: Session) -> dict:
    deprioritized_or_blocked = (
        db.query(Company).filter(Company.status.in_(["Deprioritized", "Blocked"])).order_by(Company.name).all()
    )
    most_ghosted = (
        db.query(Company).filter(Company.ghosted_count > 0).order_by(Company.ghosted_count.desc()).limit(10).all()
    )
    return {"deprioritized_or_blocked": deprioritized_or_blocked, "most_ghosted": most_ghosted}


def build_dashboard(db: Session) -> dict:
    return {
        "funnel": status_funnel(db),
        "conversion": conversion_rates(db),
        "speed": speed_to_apply(db),
        "by_source": by_source(db),
        "by_score_band": by_score_band(db),
        "flags": flag_correlation(db),
        "company_memory": company_memory_summary(db),
    }
