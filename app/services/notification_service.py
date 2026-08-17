"""
Phase 4: notifications for the confirmation queue. This is the actual
mechanism that makes a short fast-track window workable without
requiring the user to be watching the dashboard -- a one-click
approve/reject link goes out the moment an application enters the
queue, reachable from wherever the user is (see CLAUDE.md's Phase 4
design notes). Email only for now; Telegram/Discord could follow the
same is_configured()-gated pattern later.

Gracefully no-ops (logs once, doesn't raise) if SMTP isn't configured
-- same pattern as every other optional integration in this project
(Adzuna, portfolio sync).
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from ..models import JobApplication
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
