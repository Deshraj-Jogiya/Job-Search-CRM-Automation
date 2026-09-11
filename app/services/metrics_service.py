"""
Weekly-rollup metrics for /metrics -- distinct in purpose from
analytics_service.py's /analytics page (a general current-state
snapshot: status funnel, by-source, by-score-band). This page exists
specifically for the personal week-4/week-8 go/no-go review: a real
funnel with a conversion RATE at each step, segmented several ways,
and a week-over-week time series against a target line.

Same two honesty limits as analytics_service.py apply here: nothing is
inferred from an inbox, every reply/interview/offer/not-selected mark
is a self-report (see confirmation_service.mark_replied et al), and
status is a current value except where a dedicated timestamp exists.

Week bucketing happens in Python (ISO week, Monday start), not SQL --
this app supports SQLite and Postgres, whose date-truncation functions
differ; a personal job search's applications are realistically dozens
to a few hundred rows, small enough this costs nothing.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from ..models import Company, GlobalSettings, JobApplication, JobPosting, ProfileVariant, get_or_create_settings

# Below this, a resume-version reply-rate comparison is too noisy to
# flag meaningfully -- 1-2 sends either replying or not swings the rate
# from 0% to 100%.
_MIN_SAMPLE_SIZE_FOR_UNDERPERFORMANCE_FLAG = 3


def _rate(numerator: int, denominator: int) -> float | None:
    if not denominator:
        return None
    return round(100 * numerator / denominator, 1)


def _week_key(dt) -> str:
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def weekly_funnel(db: Session) -> list[dict]:
    """One row per week an application was sent (most recent first):
    sent -> replied -> interviewing (first round reached) -> offer,
    with the conversion RATE at each step, not just raw counts.
    "Final round" from the original spec isn't tracked here -- this
    schema has no per-round outcome signal, only one interviewing_at
    timestamp regardless of how many rounds followed (see FUTURE.md);
    building that out honestly needs new per-round self-report tracking,
    not a value invented from data that doesn't exist."""
    rows = (
        db.query(JobApplication.applied_at, JobApplication.replied_at, JobApplication.interviewing_at, JobApplication.offer_at)
        .filter(JobApplication.applied_at.isnot(None))
        .all()
    )
    buckets: dict[str, dict] = {}
    for applied_at, replied_at, interviewing_at, offer_at in rows:
        bucket = buckets.setdefault(_week_key(applied_at), {"sent": 0, "replied": 0, "interviewing": 0, "offers": 0})
        bucket["sent"] += 1
        if replied_at:
            bucket["replied"] += 1
        if interviewing_at:
            bucket["interviewing"] += 1
        if offer_at:
            bucket["offers"] += 1

    result = []
    for week in sorted(buckets, reverse=True):
        b = buckets[week]
        result.append(
            {
                "week": week,
                "sent": b["sent"],
                "replied": b["replied"],
                "interviewing": b["interviewing"],
                "offers": b["offers"],
                "reply_rate": _rate(b["replied"], b["sent"]),
                "interview_rate": _rate(b["interviewing"], b["replied"]),
                "offer_rate": _rate(b["offers"], b["interviewing"]),
            }
        )
    return result


def _segment(rows: list[tuple]) -> dict:
    sent = len(rows)
    replied = sum(1 for (replied_at,) in rows if replied_at is not None)
    return {"sent": sent, "replied": replied, "reply_rate": _rate(replied, sent)}


def by_resume_version(db: Session) -> list[dict]:
    results = []
    for variant in db.query(ProfileVariant).all():
        rows = (
            db.query(JobApplication.replied_at)
            .filter(JobApplication.profile_variant_id == variant.id, JobApplication.applied_at.isnot(None))
            .all()
        )
        if rows:
            results.append({"segment": variant.name, **_segment(rows)})

    unassigned_rows = (
        db.query(JobApplication.replied_at)
        .filter(JobApplication.profile_variant_id.is_(None), JobApplication.applied_at.isnot(None))
        .all()
    )
    if unassigned_rows:
        results.append({"segment": "(unassigned)", **_segment(unassigned_rows)})
    return results


def by_company_tier(db: Session) -> list[dict]:
    results = []
    for tier in ("A", "B", "C", "X", None):
        rows = (
            db.query(JobApplication.replied_at)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .join(Company, JobPosting.company_id == Company.id)
            .filter(Company.tier == tier, JobApplication.applied_at.isnot(None))
            .all()
        )
        if rows:
            results.append({"segment": tier or "unknown", **_segment(rows)})
    return results


def by_source(db: Session) -> list[dict]:
    results = []
    sources = [s for (s,) in db.query(JobPosting.source).distinct().all()]
    for source in sources:
        rows = (
            db.query(JobApplication.replied_at)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .filter(JobPosting.source == source, JobApplication.applied_at.isnot(None))
            .all()
        )
        if rows:
            results.append({"segment": source, **_segment(rows)})
    return results


def by_cap_exempt(db: Session) -> list[dict]:
    results = []
    for label, is_cap_exempt in (("Cap-exempt", True), ("Cap-subject", False)):
        rows = (
            db.query(JobApplication.replied_at)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .join(Company, JobPosting.company_id == Company.id)
            .filter(Company.is_cap_exempt == is_cap_exempt, JobApplication.applied_at.isnot(None))
            .all()
        )
        if rows:
            results.append({"segment": label, **_segment(rows)})
    return results


def by_wage_level(db: Session) -> list[dict]:
    results = []
    for level in ("I", "II", "III", "IV", None):
        rows = (
            db.query(JobApplication.replied_at)
            .join(JobPosting, JobApplication.posting_id == JobPosting.id)
            .join(Company, JobPosting.company_id == Company.id)
            .filter(Company.max_wage_level_15xx == level, JobApplication.applied_at.isnot(None))
            .all()
        )
        if rows:
            results.append({"segment": level or "unknown", **_segment(rows)})
    return results


def median_days_to_first_reply(db: Session) -> dict:
    rows = (
        db.query(JobApplication.applied_at, JobApplication.replied_at)
        .filter(JobApplication.applied_at.isnot(None), JobApplication.replied_at.isnot(None))
        .all()
    )
    if not rows:
        return {"count": 0, "median_days": None}
    days = sorted((replied - applied).total_seconds() / 86400 for applied, replied in rows)
    n = len(days)
    median = days[n // 2] if n % 2 else (days[n // 2 - 1] + days[n // 2]) / 2
    return {"count": n, "median_days": round(median, 1)}


def underperforming_resume_versions(db: Session) -> list[dict]:
    """Flags any resume version whose reply rate is below half the best
    performer -- ignores segments under _MIN_SAMPLE_SIZE_FOR_UNDERPERFORMANCE_FLAG
    sends, where a reply-rate comparison is too noisy to mean anything."""
    segments = [s for s in by_resume_version(db) if s["sent"] >= _MIN_SAMPLE_SIZE_FOR_UNDERPERFORMANCE_FLAG]
    if not segments:
        return []
    best = max(s["reply_rate"] or 0 for s in segments)
    if best <= 0:
        return []
    return [s for s in segments if (s["reply_rate"] or 0) < best / 2]


def time_series(db: Session, settings: GlobalSettings) -> dict:
    funnel_asc = list(reversed(weekly_funnel(db)))
    return {
        "weeks": [w["week"] for w in funnel_asc],
        "applications_per_week": [w["sent"] for w in funnel_asc],
        "reply_rate_per_week": [w["reply_rate"] or 0 for w in funnel_asc],
        # Weekday-based approximation from the daily /queue targets --
        # no separate weekly target setting exists, see WEIGHTING.md-
        # adjacent reasoning in queue_service.py for the daily ones.
        "target_applications_per_week_min": settings.daily_application_target_min * 5,
        "target_applications_per_week_max": settings.daily_application_target_max * 5,
    }


def build_dashboard(db: Session) -> dict:
    settings = get_or_create_settings(db)
    return {
        "weekly_funnel": weekly_funnel(db),
        "by_resume_version": by_resume_version(db),
        "by_company_tier": by_company_tier(db),
        "by_source": by_source(db),
        "by_cap_exempt": by_cap_exempt(db),
        "by_wage_level": by_wage_level(db),
        "median_days_to_first_reply": median_days_to_first_reply(db),
        "underperforming": underperforming_resume_versions(db),
        "time_series": time_series(db, settings),
    }
