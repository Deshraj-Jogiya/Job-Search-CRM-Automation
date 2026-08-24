"""
Recruiter outreach. Deliberately avoids the common failure mode of
searching the web for a recruiter and then GUESSING their email via a
first.last@domain pattern. This version never guesses: the recipient
is entered by the user (they already know who they're reaching out
to), and email_verified is a syntax + MX-record sanity check on what
the user supplied, not a claim that the address is real -- it's
surfaced as a warning, not a hard block.

Safety-critical distinction from the confirmation-gated auto-apply
queue: sending a real email to a real external person is immediately
irreversible and externally visible, unlike an application's
"Approved" status (which just flips an internal flag -- there's no
submission engine to act on it). So outreach has NO timers and NO
auto-send-on-timeout anywhere in this file -- Draft -> Approved -> Sent
always requires two separate, explicit human clicks, with the actual
send only ever happening inside send_outreach(), called directly from
a live request.

LinkedIn channels (connection note / InMail) are drafted here but
never sent automatically -- LinkedIn automation of any kind was
explicitly ruled out elsewhere in this project's design (account risk).
The user copies the drafted note into LinkedIn themselves and confirms
via mark_sent_manually().
"""

import re
import threading
from datetime import timedelta

import dns.resolver
from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import JobApplication, OutreachMessage, get_or_create_settings
from .activity_logger import log_activity
from .email_utils import is_smtp_configured, send_email
from .llm import get_llm_provider
from .matching_service import get_profile_content_for_application

_EMAIL_RE = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")

_VALID_CHANNELS = ("email", "linkedin_connection", "linkedin_inmail")

# Guards the daily-cap check-then-send-then-commit sequence in
# send_outreach() -- without it, two concurrent requests in this
# process could both read the same under-cap count and both send,
# exceeding daily_outreach_cap. A single-process in-memory lock is
# sufficient here since this app runs as one process (see README/
# ARCHITECTURE for the local-first deployment model).
_send_lock = threading.Lock()


class OutreachServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def verify_email_address(email: str) -> bool:
    """Syntax + MX-record check only -- confirms the domain can
    plausibly receive mail, not that this specific mailbox exists.
    Never a guarantee; see module docstring."""
    if not email or not _EMAIL_RE.match(email.strip()):
        return False
    domain = email.strip().rsplit("@", 1)[1]
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=5)
        return len(answers) > 0
    except Exception:
        return False


def _draft_note_text(profile_content: dict, posting, recipient_name: str, channel: str) -> str:
    if channel == "linkedin_connection":
        instruction = (
            "Write a LinkedIn connection request note, STRICTLY under 300 characters total including spaces, "
            "warm and specific, mentioning the role and why the candidate is reaching out."
        )
        max_tokens = 150
    else:
        instruction = (
            "Write a short, professional outreach message (3-5 sentences) introducing the candidate and "
            "expressing genuine interest in the role, referencing something specific from the job description."
        )
        max_tokens = 400

    llm = get_llm_provider()
    text = llm.complete_text(
        system=(
            "You write concise, genuine-sounding professional outreach messages. No generic template phrases "
            "('I hope this message finds you well', 'I am writing to express interest'). No greeting or "
            "sign-off/signature -- just the message body."
        ),
        prompt=(
            f"{instruction}\n\n"
            f"Candidate: {profile_content.get('name')}\n"
            f"Candidate summary: {profile_content.get('summary')}\n"
            f"Recipient name: {recipient_name or 'there'}\n"
            f"Target company: {posting.company_name_raw}\n"
            f"Target role: {posting.job_title}\n"
            f"Job description excerpt: {(posting.job_description or '')[:800]}"
        ),
        temperature=0.5,
        max_tokens=max_tokens,
    )
    return text.strip()


def draft_outreach_message(
    db: Session, application_id: int, recipient_name: str, recipient_address: str, channel: str
) -> OutreachMessage:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise OutreachServiceError(f"Application {application_id} not found.")
    if channel not in _VALID_CHANNELS:
        raise OutreachServiceError(f"Unknown channel '{channel}'.")
    if channel == "email" and not recipient_address:
        raise OutreachServiceError("An email address is required for the email channel.")

    profile_content, _ = get_profile_content_for_application(db, application)
    posting = application.posting

    try:
        body = _draft_note_text(profile_content, posting, recipient_name, channel)
    except Exception as e:
        raise OutreachServiceError(f"Drafting failed: {e}") from e

    subject = (
        f"Interested in the {posting.job_title} role at {posting.company_name_raw}"
        if channel == "email"
        else None
    )
    email_verified = verify_email_address(recipient_address) if channel == "email" else False

    message = OutreachMessage(
        application_id=application.id,
        channel=channel,
        recipient_name=recipient_name or None,
        recipient_address=recipient_address or None,
        subject=subject,
        body=body,
        status="Draft",
        email_verified=email_verified,
    )
    db.add(message)
    db.commit()
    db.refresh(message)

    log_activity(db, f"Drafted {channel} outreach for '{posting.job_title}' at {posting.company_name_raw}.", "INFO")
    return message


def approve_outreach(db: Session, message_id: int) -> OutreachMessage:
    message = db.query(OutreachMessage).filter(OutreachMessage.id == message_id).first()
    if not message:
        raise OutreachServiceError(f"Outreach message {message_id} not found.")
    if message.status != "Draft":
        raise OutreachServiceError(f"Message is '{message.status}', not a Draft.")
    message.status = "Approved"
    db.commit()
    log_activity(db, f"Approved outreach message {message_id}.", "INFO")
    return message


def reject_outreach(db: Session, message_id: int) -> OutreachMessage:
    message = db.query(OutreachMessage).filter(OutreachMessage.id == message_id).first()
    if not message:
        raise OutreachServiceError(f"Outreach message {message_id} not found.")
    if message.status == "Sent":
        raise OutreachServiceError("This message has already been sent and can't be un-sent.")
    message.status = "Rejected"
    db.commit()
    log_activity(db, f"Rejected outreach message {message_id}.", "INFO")
    return message


def sent_count_last_24h(db: Session) -> int:
    cutoff = utcnow() - timedelta(hours=24)
    return (
        db.query(OutreachMessage)
        .filter(OutreachMessage.status == "Sent", OutreachMessage.sent_at >= cutoff)
        .count()
    )


def send_outreach(db: Session, message_id: int) -> OutreachMessage:
    """The only place a real email actually goes out. Only ever called
    from a live request triggered by an explicit user click -- never
    from the scheduler, never on a timer. See module docstring."""
    message = db.query(OutreachMessage).filter(OutreachMessage.id == message_id).first()
    if not message:
        raise OutreachServiceError(f"Outreach message {message_id} not found.")
    if message.channel != "email":
        raise OutreachServiceError(
            "Only the email channel sends automatically -- LinkedIn notes are copied manually, "
            "then confirmed via Mark as Sent."
        )
    if message.status != "Approved":
        raise OutreachServiceError(f"Message is '{message.status}', not Approved.")

    if not is_smtp_configured():
        raise OutreachServiceError("SMTP is not configured -- add SMTP_USER/SMTP_PASSWORD to .env first.")

    with _send_lock:
        settings = get_or_create_settings(db)
        sent_today = sent_count_last_24h(db)
        if sent_today >= settings.daily_outreach_cap:
            raise OutreachServiceError(f"Daily outreach cap ({settings.daily_outreach_cap}) already reached today.")

        try:
            send_email(message.recipient_address, message.subject, message.body)
        except Exception as e:
            raise OutreachServiceError(f"Send failed: {e}") from e

        message.status = "Sent"
        message.sent_at = utcnow()
        db.commit()

    log_activity(db, f"Sent outreach email to {message.recipient_address}.", "INFO")
    return message


def mark_sent_manually(db: Session, message_id: int) -> OutreachMessage:
    """For LinkedIn channels: the user copies the drafted note into
    LinkedIn themselves and confirms here once they've actually sent
    it. Never automated -- see module docstring."""
    message = db.query(OutreachMessage).filter(OutreachMessage.id == message_id).first()
    if not message:
        raise OutreachServiceError(f"Outreach message {message_id} not found.")
    if message.channel == "email":
        raise OutreachServiceError("Email messages use Send Now, not Mark as Sent.")
    if message.status != "Approved":
        raise OutreachServiceError(f"Message is '{message.status}', not Approved.")

    message.status = "Sent"
    message.sent_at = utcnow()
    db.commit()
    log_activity(db, f"Marked {message.channel} outreach message {message_id} as sent (manual).", "INFO")
    return message
