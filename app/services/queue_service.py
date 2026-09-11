"""
The daily triage queue (/queue). Three tabs -- Cap-exempt, Tier A/B,
Everything else -- each sorted by score_breakdown.total desc, each
excluding anything already decided (a terminal status), already
skipped, or hard-gated by a sponsorship-blocked JD (see
sponsorship_signals.py -- these are never deleted, just never shown
here by default).

Sorting happens in Python, not SQL: score_breakdown is a JSON column
and this app supports both SQLite and Postgres (see WEIGHTING.md/
Alembic migration notes on staying dialect-generic) -- a personal job
search's queue is realistically dozens to a few hundred rows, small
enough that sorting after a bounded query costs nothing worth a
dialect-specific JSON-path query for.
"""

from sqlalchemy.orm import Session, joinedload

from ..database import utcnow
from ..models import Company, JobApplication, JobPosting, get_or_create_settings
from .activity_logger import log_activity
from .company_tier import derive_tier
from .scoring_service import recompute_score_breakdown

_TERMINAL_STATUSES = ("Applied", "Rejected", "Interviewing", "Offer", "Not Selected")

SKIP_REASONS = ("not_interested", "low_priority", "duplicate_role", "bad_timing", "other")


class QueueServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def _active_applications_query(db: Session):
    return (
        db.query(JobApplication)
        .join(JobPosting, JobApplication.posting_id == JobPosting.id)
        .options(joinedload(JobApplication.posting).joinedload(JobPosting.company))
        .filter(
            JobApplication.status.notin_(_TERMINAL_STATUSES),
            JobApplication.skipped_at.is_(None),
            JobPosting.sponsorship_blocked.is_(False),
        )
    )


def _score_of(application: JobApplication) -> int:
    if application.score_breakdown and "total" in application.score_breakdown:
        return application.score_breakdown["total"]
    return application.match_score or 0


def _sorted_by_score(applications: list[JobApplication]) -> list[JobApplication]:
    return sorted(applications, key=_score_of, reverse=True)


def build_queue(db: Session) -> dict:
    applications = _active_applications_query(db).all()

    cap_exempt = [a for a in applications if a.posting.company and a.posting.company.is_cap_exempt]
    tier_ab = [
        a for a in applications
        if a not in cap_exempt and a.posting.company and a.posting.company.tier in ("A", "B")
    ]
    everything_else = [a for a in applications if a not in cap_exempt and a not in tier_ab]

    return {
        "cap_exempt": _sorted_by_score(cap_exempt),
        "tier_ab": _sorted_by_score(tier_ab),
        "everything_else": _sorted_by_score(everything_else),
    }


def skip_application(db: Session, application_id: int, reason: str) -> JobApplication:
    if reason not in SKIP_REASONS:
        raise QueueServiceError(f"Unknown skip reason '{reason}'.")
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise QueueServiceError(f"Application {application_id} not found.")
    if application.status in _TERMINAL_STATUSES:
        raise QueueServiceError(f"Application is already '{application.status}' -- nothing to skip.")

    application.skipped_at = utcnow()
    application.skip_reason = reason
    db.commit()
    log_activity(
        db,
        f"Skipped '{application.posting.job_title}' at {application.posting.company_name_raw}" f" ({reason}).",
        "INFO",
    )
    return application


def unskip_application(db: Session, application_id: int) -> JobApplication:
    """Skip is deliberately soft/reversible -- see module docstring."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise QueueServiceError(f"Application {application_id} not found.")
    application.skipped_at = None
    application.skip_reason = None
    db.commit()
    return application


def mark_not_a_fit(db: Session, application_id: int, reason: str) -> JobApplication:
    """Distinct from skip_application: this is a firmer, terminal
    decision -- reuses the existing "Rejected" status (same downstream
    sweep behavior as confirmation_service.reject_application), but
    unlike that function, this is callable from ANY pre-terminal status
    since /queue triages applications well before the confirmation
    stage those functions guard."""
    if reason not in SKIP_REASONS:
        raise QueueServiceError(f"Unknown reason '{reason}'.")
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise QueueServiceError(f"Application {application_id} not found.")
    if application.status in _TERMINAL_STATUSES:
        raise QueueServiceError(f"Application is already '{application.status}'.")

    application.status = "Rejected"
    application.rejected_at = utcnow()
    application.skip_reason = reason
    db.commit()
    log_activity(
        db,
        f"Marked 'Not a fit': '{application.posting.job_title}' at {application.posting.company_name_raw}"
        f" ({reason}).",
        "INFO",
    )
    return application


def recompute_all_tiers(db: Session) -> dict:
    """Idempotent -- always recomputes from current Company data rather
    than only ever moving forward, so a company's tier correctly drops
    if e.g. a bad USCIS/LCA data point gets corrected upstream."""
    settings = get_or_create_settings(db)
    companies = db.query(Company).all()
    now = utcnow()
    changed = 0
    for company in companies:
        new_tier = derive_tier(company, settings)
        if company.tier != new_tier:
            changed += 1
        company.tier = new_tier
        company.tier_computed_at = now
    db.commit()
    log_activity(db, f"Recomputed tier for {len(companies)} companies ({changed} changed).", "INFO")
    return {"companies_total": len(companies), "companies_changed": changed}


def recompute_all_scores(db: Session) -> dict:
    """Mechanical-only rescoring (no LLM call) across every application
    -- safe to run in bulk any time tier/wage/signal data changes. See
    scoring_service.recompute_score_breakdown."""
    applications = db.query(JobApplication).options(joinedload(JobApplication.posting)).all()
    for application in applications:
        recompute_score_breakdown(db, application)
    log_activity(db, f"Recomputed score_breakdown for {len(applications)} applications.", "INFO")
    return {"applications_total": len(applications)}
