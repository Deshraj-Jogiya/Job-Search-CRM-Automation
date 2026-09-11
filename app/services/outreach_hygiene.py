"""
Outreach hygiene caps: never send more than one cold message to the
same person, and never more than a few to the same company within a
rolling window. Checked at DRAFT time (not send time) -- the whole
point is to stop a duplicate message from ever being written, not to
catch it after the fact. Both caps are config-driven
(GlobalSettings.outreach_person_lifetime_cap /
outreach_company_cap_count / outreach_company_cap_days), not hardcoded.

Only counts messages that actually reached "Sent" -- a Draft that was
never approved/was rejected shouldn't count against a real-world cap
that exists to protect the recipient from repeated contact, since
nothing was ever actually sent to them.
"""

from datetime import timedelta

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import GlobalSettings, JobApplication, JobPosting, OutreachMessage, get_or_create_settings


class OutreachCapViolation(Exception):
    """Raised by check_caps() when a cap is hit and no override reason
    was supplied -- callers show this message and offer the override
    flow, they don't silently swallow it."""


def _person_sent_count(db: Session, recipient_address: str) -> int:
    if not recipient_address:
        return 0
    return (
        db.query(OutreachMessage)
        .filter(OutreachMessage.recipient_address == recipient_address, OutreachMessage.status == "Sent")
        .count()
    )


def _company_sent_count_in_window(db: Session, company_id: int, days: int) -> int:
    if not company_id:
        return 0
    since = utcnow() - timedelta(days=days)
    return (
        db.query(OutreachMessage)
        .join(JobApplication, OutreachMessage.application_id == JobApplication.id)
        .join(JobPosting, JobApplication.posting_id == JobPosting.id)
        .filter(
            JobPosting.company_id == company_id,
            OutreachMessage.status == "Sent",
            OutreachMessage.sent_at.isnot(None),
            OutreachMessage.sent_at >= since,
        )
        .count()
    )


def check_caps(
    db: Session, recipient_address: str, company_id: int | None, settings: GlobalSettings | None = None
) -> None:
    """Raises OutreachCapViolation with a clear, specific message if
    either cap is already at or past its limit. Callers that want to
    proceed anyway route through an explicit override (see
    outreach_service.draft_outreach_message's override_reason param),
    never silently."""
    settings = settings or get_or_create_settings(db)

    person_count = _person_sent_count(db, recipient_address)
    if person_count >= settings.outreach_person_lifetime_cap:
        raise OutreachCapViolation(
            f"Already sent {person_count} message(s) to {recipient_address} -- "
            f"lifetime cap is {settings.outreach_person_lifetime_cap}."
        )

    if company_id:
        company_count = _company_sent_count_in_window(db, company_id, settings.outreach_company_cap_days)
        if company_count >= settings.outreach_company_cap_count:
            raise OutreachCapViolation(
                f"Already sent {company_count} message(s) to this company in the last "
                f"{settings.outreach_company_cap_days} days -- cap is {settings.outreach_company_cap_count}."
            )
