"""
Phase 4: notifications for the confirmation queue.

Deliberately not one-email-per-application -- an email per queued
application becomes a flood the moment several queue at once, so this
splits into two paths instead:

- send_confirmation_notification(): an immediate, individual one-click
  email. Only called for fast-track applications (see
  confirmation_service.evaluate_and_enqueue) -- rare by design, and the
  one case where per-item speed still matters more than batching.
- send_digest(): a single periodic email summarizing everything else
  that's queued and not yet notified about, pointing at the bulk
  review page (app/routers/jobs.py's /jobs/review) rather than trying
  to carry approve/reject actions for every item inline.

Email only for now; Telegram/Discord could follow the same
is_configured()-gated pattern later. Gracefully no-ops (logs once,
doesn't raise) if SMTP isn't configured -- same pattern as every other
optional integration in this project (Adzuna, portfolio sync).
"""

import os
from datetime import timedelta

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import JobApplication, get_or_create_settings
from . import confirmation_tokens
from .activity_logger import log_activity
from .email_utils import is_smtp_configured, send_email as _send_email


def is_configured() -> bool:
    return is_smtp_configured()


def _base_url() -> str:
    configured = os.getenv("APP_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return f"http://localhost:{os.getenv('PORT', '8000')}"


def send_confirmation_notification(db: Session, application: JobApplication) -> bool:
    if not is_configured():
        log_activity(db, "Skipping confirmation email: SMTP not configured.", "WARNING")
        return False

    to_addr = os.getenv("SMTP_USER")
    posting = application.posting
    token = confirmation_tokens.generate_token(application.id)
    link = f"{_base_url()}/confirm/{application.id}?token={token}"

    if application.status == "Needs Review":
        subject = f"[Needs Review] {posting.job_title} at {posting.company_name_raw}"
        deadline_line = "No timeout -- this will wait for you no matter how long it takes."
    else:
        subject = f"Approve? {posting.job_title} at {posting.company_name_raw} ({application.match_score}% match)"
        deadline_line = (
            f"Auto-proceeds at {application.confirmation_deadline.strftime('%Y-%m-%d %H:%M')} UTC "
            "if you don't act -- click below to decide sooner."
        )

    reason_line = f"\nFlagged: {application.attention_reason}\n" if application.attention_reason else ""

    body = (
        f"{posting.job_title} at {posting.company_name_raw}\n"
        f"Match score: {application.match_score}%\n"
        f"{reason_line}"
        f"{deadline_line}\n\n"
        f"Review and decide: {link}\n"
    )

    try:
        _send_email(to_addr, subject, body)
        log_activity(db, f"Sent confirmation email for '{posting.job_title}' at {posting.company_name_raw}.", "INFO")
        return True
    except Exception as e:
        log_activity(db, f"Failed to send confirmation email: {e}", "ERROR")
        return False


def send_autofill_ready_notification(db: Session, application: JobApplication) -> bool:
    """Called from confirmation_service's auto-launch-on-clean-tailor
    path. Unlike the two notifications above, there's no remote action
    to link to: the real, already-filled browser window is open locally
    on this machine, waiting for the human to review and click its own
    submit button. This just makes sure they know to go look, since
    they might not be watching the dashboard when tailoring finishes."""
    if not is_configured():
        log_activity(db, "Skipping autofill-ready email: SMTP not configured.", "WARNING")
        return False

    to_addr = os.getenv("SMTP_USER")
    posting = application.posting
    subject = f"Ready to review: {posting.job_title} at {posting.company_name_raw}"
    body = (
        f"{posting.job_title} at {posting.company_name_raw}\n"
        f"Match score: {application.match_score}%\n\n"
        "This one tailored clean, so a real browser window has been opened on this machine with "
        "the application form pre-filled. Nothing has been submitted -- review every field, "
        "including any left blank on purpose, then click Submit in that window yourself when ready.\n"
    )

    try:
        _send_email(to_addr, subject, body)
        log_activity(
            db, f"Sent autofill-ready email for '{posting.job_title}' at {posting.company_name_raw}.", "INFO"
        )
        return True
    except Exception as e:
        log_activity(db, f"Failed to send autofill-ready email: {e}", "ERROR")
        return False


def send_digest(db: Session) -> int:
    """Batches every application that's queued (Pending Confirmation or
    Needs Review) and hasn't been notified about yet into ONE email,
    pointing at the bulk review page. Called from the scheduler tick;
    a no-op if nothing new has queued, or if the digest interval hasn't
    elapsed yet. Returns the number of applications included."""
    if not is_configured():
        return 0

    settings = get_or_create_settings(db)
    now = utcnow()
    if settings.last_digest_sent_at is not None:
        elapsed = now - settings.last_digest_sent_at
        if elapsed < timedelta(minutes=settings.notification_digest_interval_minutes):
            return 0

    pending = (
        db.query(JobApplication)
        .filter(
            JobApplication.status.in_(["Pending Confirmation", "Needs Review"]),
            JobApplication.notification_sent == False,  # noqa: E712
        )
        .all()
    )
    if not pending:
        return 0

    to_addr = os.getenv("SMTP_USER")
    review_link = f"{_base_url()}/jobs/review"

    lines = [f"{len(pending)} application(s) awaiting your review:\n"]
    for application in pending:
        posting = application.posting
        flag = f" [FLAGGED: {application.attention_reason}]" if application.attention_reason else ""
        lines.append(f"- {posting.job_title} at {posting.company_name_raw} ({application.match_score}% match){flag}")
    lines.append(f"\nReview and decide: {review_link}\n")
    body = "\n".join(lines)

    subject = f"{len(pending)} application(s) awaiting review"

    try:
        _send_email(to_addr, subject, body)
    except Exception as e:
        log_activity(db, f"Failed to send digest email: {e}", "ERROR")
        return 0

    for application in pending:
        application.notification_sent = True
    settings.last_digest_sent_at = now
    db.commit()

    log_activity(db, f"Sent digest email covering {len(pending)} application(s).", "INFO")
    return len(pending)
