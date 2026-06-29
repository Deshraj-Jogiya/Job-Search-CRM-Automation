import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from ..models import JobApplication
from ..database import SessionLocal
from .activity_logger import log_activity

def decode_mime_words(s):
    """Clean MIME encoded strings."""
    clean_parts = []
    try:
        parts = decode_header(s)
        for part, encoding in parts:
            if isinstance(part, bytes):
                clean_parts.append(part.decode(encoding or "utf-8", errors="ignore"))
            else:
                clean_parts.append(str(part))
    except Exception:
        return s
    return "".join(clean_parts)

def parse_body(msg):
    """Extract clean string body from email message object."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return body

def scan_inbox_for_updates():
    """Scans IMAP email inbox for application replies, updating statuses in the database."""
    imap_host = os.getenv("IMAP_HOST")
    imap_user = os.getenv("IMAP_USER")
    imap_password = os.getenv("IMAP_PASSWORD") # App password
    
    if not imap_host or not imap_user or not imap_password:
        print("Email status monitor: Credentials not fully configured in .env. Skipping scan.")
        return
        
    db = SessionLocal()
    try:
        # Connect to IMAP server
        print(f"Connecting to IMAP inbox: {imap_host}...")
        mail = imaplib.IMAP4_SSL(imap_host, port=993)
        mail.login(imap_user, imap_password)
        mail.select("INBOX")
        
        # Search for emails in the last 7 days to keep it fast
        date_cutoff = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
        status, response = mail.search(None, f'SINCE {date_cutoff}')
        
        if status != "OK":
            print("Failed to query inbox.")
            return
            
        message_ids = response[0].split()
        print(f"Found {len(message_ids)} emails in the past 7 days. Analyzing...")
        
        # Load active applications
        active_apps = db.query(JobApplication).filter(
            JobApplication.status.in_(["Applied", "Interviewing"])
        ).all()
        
        if not active_apps:
            print("No active job applications tracked as 'Applied' or 'Interviewing'. Skipping email matching.")
            return
            
        # Parse emails from newest to oldest
        for msg_id in reversed(message_ids):
            status, data = mail.fetch(msg_id, "(RFC822)")
            if status != "OK":
                continue
                
            raw_email = data[0][1]
            msg = email.message_from_bytes(raw_email)
            
            subject = decode_mime_words(msg.get("Subject", ""))
            sender = decode_mime_words(msg.get("From", ""))
            body = parse_body(msg)
            
            combined_text = (subject + " " + sender + " " + body).lower()
            
            # Check if any active company name is mentioned
            for app in active_apps:
                company = app.company_name.lower()
                # Ensure the company name matches as a word boundary to prevent partial substring false positives
                if company in combined_text:
                    print(f"Matched email from '{sender}' containing subject '{subject}' to company '{app.company_name}'")
                    
                    # Classify email using AI
                    from . import ai_service
                    classification = ai_service.classify_email_response(subject, body)
                    intent = classification.get("intent", "other")
                    reason = classification.get("reason", "No reason details extracted.")
                    
                    status_updated = False
                    new_status = app.status
                    
                    reason_snippet = f"\n[Auto Email Status Update - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n"
                    reason_snippet += f"Sender: {sender}\nSubject: {subject}\n"
                    reason_snippet += f"AI Intent Classification: {intent.upper()}\n"
                    reason_snippet += f"Details: {reason}\n"
                    
                    # Map intents to pipeline statuses
                    if intent == "rejection":
                        new_status = "Rejected"
                        status_updated = True
                    elif intent == "interview":
                        new_status = "Interviewing"
                        status_updated = True
                    elif intent == "assessment":
                        new_status = "Needs Review"
                        status_updated = True
                        reason_snippet += "Action Required: Complete coding assessment/test.\n"
                    elif intent == "additional_requirements":
                        new_status = "Needs Review"
                        status_updated = True
                        reason_snippet += "Action Required: Provide additional application files/details.\n"
                    elif intent == "confirmation":
                        reason_snippet += "Status unchanged. Received application confirmation email.\n"
                        status_updated = True # Log the confirmation in the notes
                    else:
                        reason_snippet += "Status unchanged. Generic notification email.\n"
                        status_updated = True
                        
                    if status_updated:
                        if new_status == "Rejected":
                            log_activity(db, f"Auto-deleting rejected job '{app.job_title}' at '{app.company_name}' (Intent: REJECTION)", "INFO")
                            db.query(TailoredDocument).filter(TailoredDocument.job_id == app.id).delete()
                            db.delete(app)
                            db.commit()
                            continue
                            
                        if new_status != app.status:
                            log_activity(db, f"Updated '{app.job_title}' at '{app.company_name}' status from '{app.status}' to '{new_status}' (Intent: {intent.upper()})", "INFO")
                            app.status = new_status
                        else:
                            log_activity(db, f"Logged company update email for '{app.job_title}' at '{app.company_name}' (Intent: {intent.upper()})", "INFO")
                        
                        # Set or clear attention reason
                        if new_status == "Needs Review":
                            app.attention_reason = f"Recruiter {intent}: {reason[:200]}"
                        else:
                            app.attention_reason = None

                        # Append details to notes log
                        app.notes = (app.notes or "") + reason_snippet
                        db.commit()
                        
        mail.logout()
    except Exception as e:
        print(f"Error in Email status monitor: {e}")
    finally:
        db.close()
