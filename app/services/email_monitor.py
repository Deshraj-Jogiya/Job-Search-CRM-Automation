import os
import imaplib
import email
from email.header import decode_header
from datetime import datetime, timedelta
from sqlalchemy.orm import Session
from ..models import JobApplication
from ..database import SessionLocal

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
                    
                    # Keywords for rejections
                    rejection_keywords = [
                        "unfortunate", "not selected", "thank you for your interest", 
                        "decision to move forward with other", "pursue other candidates", 
                        "not moving forward", "decided not to proceed", "positions filled"
                    ]
                    
                    # Keywords for interview calls
                    interview_keywords = [
                        "schedule a call", "interview", "phone screening", "availability to speak", 
                        "chat about your application", "invitation to interview", "next steps in the process",
                        "technical assessment", "online test"
                    ]
                    
                    status_updated = False
                    new_status = None
                    reason_snippet = f"\n[Auto Email Status Update - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\nSender: {sender}\nSubject: {subject}\n"
                    
                    # 1. Check rejections
                    if any(key in combined_text for key in rejection_keywords):
                        new_status = "Rejected"
                        status_updated = True
                        reason_snippet += "Status auto-changed to 'Rejected' based on email keywords.\n"
                    # 2. Check interviews
                    elif any(key in combined_text for key in interview_keywords) and app.status == "Applied":
                        new_status = "Interviewing"
                        status_updated = True
                        reason_snippet += "Status auto-changed to 'Interviewing' based on email keywords.\n"
                        
                    if status_updated and new_status != app.status:
                        print(f"Updating '{app.job_title}' at '{app.company_name}' status from '{app.status}' to '{new_status}'")
                        app.status = new_status
                        # Append email details to notes
                        app.notes = (app.notes or "") + reason_snippet
                        db.commit()
                        
        mail.logout()
    except Exception as e:
        print(f"Error in Email status monitor: {e}")
    finally:
        db.close()
