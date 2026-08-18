"""
Phase 4: notifications for the confirmation queue.

Two paths, deliberately not one-email-per-application (see CLAUDE.md's
2026-08-17 notification volume revision -- the first version of this
sent an individual email for every queued application, which is a
disaster the moment several queue at once):

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
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from ..models import JobApplication, get_or_create_settings
from . import confirmation_tokens
from .activity_logger import log_activity


def is_configured() -> bool:
    return bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def _base_url() -> str:
    configured = os.getenv("APP_BASE_URL")
    if configured:
        return configured.rstrip("/")
    return f"http://localhost:{os.getenv('PORT', '8000')}"


def _send_email(to_addr: str, subject: str, body: str) -> bool:
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "plain", "utf-8"))

    server = smtplib.SMTP(smtp_server, smtp_port, timeout=15)
    try:
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
    finally:
        server.quit()
    return True


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


def send_digest(db: Session) -> int:
    """Batches every application that's queued (Pending Confirmation or
    Needs Review) and hasn't been notified about yet into ONE email,
    pointing at the bulk review page. Called from the scheduler tick;
    a no-op if nothing new has queued, or if the digest interval hasn't
    elapsed yet. Returns the number of applications included."""
    if not is_configured():
        return 0

    settings = get_or_create_settings(db)
    now = datetime.utcnow()
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
