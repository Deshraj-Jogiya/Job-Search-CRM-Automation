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


def count_applications_by_status(db: Session, status: str) -> str:
    """Counts real JobApplication rows with the given status
    (case-insensitive exact match)."""
    count = (
        db.query(JobApplication)
        .filter(JobApplication.status.ilike(status.strip()))
        .count()
    )
    return f"{count} application(s) with status '{status}'."
