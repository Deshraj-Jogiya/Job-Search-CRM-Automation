"""
Shared SMTP send helper -- used by notification_service (confirmation
queue emails) and outreach_service. Kept separate so both
callers share one implementation of "is SMTP configured" and "actually
send," rather than duplicating smtplib boilerplate.
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText


def is_smtp_configured() -> bool:
    return bool(os.getenv("SMTP_USER") and os.getenv("SMTP_PASSWORD"))


def send_email(to_addr: str, subject: str, body: str) -> bool:
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
