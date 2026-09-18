"""Real, read-only data-access helpers used as tools by research_agent.py.
Kept separate from the agent module so they're independently testable
against a real database session, and reusable outside the agent."""

from sqlalchemy.orm import Session

from ..models import Company, JobApplication


def search_company(db: Session, name: str) -> str:
    """Looks up a company by (case-insensitive) name and returns its
    real recorded status/sponsorship data, or a not-found message."""
    company = (
        db.query(Company)
        .filter(Company.name.ilike(f"%{name.strip()}%"))
        .first()
    )
    if company is None:
        return f"No company found matching '{name}'."

    bits = [f"{company.name}: status={company.status}"]
    if company.status_reason:
        bits.append(f"reason={company.status_reason}")
    if company.ghosted_count:
        bits.append(f"ghosted_count={company.ghosted_count}")
    return ", ".join(bits)


#  A real bug found live 2026-09-18: the dashboard's own conversion_rates()
# counts "applied" as applied_at.isnot(None) -- a funnel milestone that
# stays true forever once reached, even after the application later
# progresses to Interviewing or Offer. This tool used to do a literal
# CURRENT-status match instead, so an application that had moved on to
# "Interviewing" no longer counted as "applied" here even though it
# obviously still had -- asking "how many applications are marked
# applied?" through the research agent gave a different number (1) than
# the dashboard's own Applied stat (2) for the exact same real data, a
# real "looks right, actually inconsistent" bug, not a hallucination.
# Only these three funnel milestones get the timestamp-based, cumulative
# definition; every other status ("Needs Review", "Approved", "Rejected",
# etc.) has no such "stays true after progressing" semantic, so a literal
# current-status match remains correct there.
_FUNNEL_MILESTONE_COLUMNS = {
    "applied": JobApplication.applied_at,
    "interviewing": JobApplication.interviewing_at,
    "offer": JobApplication.offer_at,
}


def count_applications_by_status(db: Session, status: str) -> str:
    """Counts real JobApplication rows matching the given status. For the
    three funnel milestones (applied/interviewing/offer), matches the same
    "reached this stage at least once" definition analytics_service's own
    dashboard stats use, not just the CURRENT status string."""
    status_key = status.strip().lower()
    milestone_column = _FUNNEL_MILESTONE_COLUMNS.get(status_key)
    if milestone_column is not None:
        count = db.query(JobApplication).filter(milestone_column.isnot(None)).count()
    else:
        count = (
            db.query(JobApplication)
            .filter(JobApplication.status.ilike(status.strip()))
            .count()
        )
    return f"{count} application(s) with status '{status}'."
