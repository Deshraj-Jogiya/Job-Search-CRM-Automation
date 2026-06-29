import os
import re
import urllib.request
import urllib.parse
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from sqlalchemy.orm import Session
from ..database import SessionLocal
from ..models import JobApplication
from . import ai_service

def search_recruiter(company_name: str) -> tuple[str, str]:
    """Search Google for recruiter/hiring manager at the company and return name & LinkedIn URL."""
    clean_company = re.sub(r'(?i)(inc|llc|ltd|co|corp|solutions|technologies|group)', '', company_name).strip()
    query = f'site:linkedin.com/in "recruiter" OR "hiring manager" "{clean_company}"'
    url = f"https://www.google.com/search?q={urllib.parse.quote(query)}"
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36'
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        print(f"Sourcing recruiter for '{company_name}' via: {url}")
        with urllib.request.urlopen(req, timeout=10) as response:
            html_content = response.read().decode('utf-8', errors='ignore')
            
            # Extract LinkedIn profile links
            links = re.findall(r'https://www\.linkedin\.com/in/[a-zA-Z0-9\-_]+', html_content)
            if links:
                linkedin_url = links[0]
                slug = linkedin_url.split('/in/')[-1].split('/')[0].split('?')[0]
                name = slug.replace('-', ' ').replace('_', ' ').title()
                # Strip trailing numbers
                name = re.sub(r'\d+', '', name).strip()
                return name, linkedin_url
    except Exception as e:
        print(f"Error searching recruiter for '{company_name}': {e}")
    return "", ""

def guess_recruiter_email(recruiter_name: str, company_name: str) -> str:
    """Guess a recruiter email address using first.last@companydomain.com heuristic."""
    if not recruiter_name:
        return ""
    
    # Standardize company domain name
    domain = company_name.lower().strip()
    domain = re.sub(r'(?i)(inc|llc|ltd|co|corp|solutions|technologies|group)', '', domain).strip()
    domain = domain.replace(' ', '').replace('&', '')
    domain = f"{domain}.com"
    
    # Standardize name
    parts = recruiter_name.lower().split()
    if len(parts) >= 2:
        first = parts[0]
        last = parts[-1]
        return f"{first}.{last}@{domain}"
    elif len(parts) == 1:
        return f"{parts[0]}@{domain}"
    return ""

def send_outreach_email(to_email: str, subject: str, body: str) -> bool:
    """Send an outreach email using Python smtplib."""
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")
    
    if not smtp_user or not smtp_password:
        print("SMTP user or password not set in .env. Skipping automated email.")
        return False
        
    try:
        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = to_email
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        server.quit()
        print(f"SUCCESS: Automated outreach email sent to {to_email}")
        return True
    except Exception as e:
        print(f"Failed to send email to {to_email}: {e}")
        return False

def trigger_recruiter_sourcing_and_outreach(job_id: int):
    """Sourced recruiter, generates connection and InMail notes, guesses email, and auto-mails."""
    db = SessionLocal()
    try:
        job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
        if not job:
            return
            
        print(f"Starting recruiter sourcing workflow for Job ID {job_id} ('{job.company_name}')...")
        
        # 1. Search Google/LinkedIn for recruiter details
        name, url = search_recruiter(job.company_name)
        if name and url:
            job.recruiter_name = name
            job.recruiter_linkedin = url
            print(f"Recruiter found: {name} ({url})")
            
            # 2. Guess email address
            email_guess = guess_recruiter_email(name, job.company_name)
            job.recruiter_email = email_guess
            print(f"Heuristically guessed recruiter email: {email_guess}")
        else:
            print("No matching recruiter profile discovered via Google Search.")
            
        # 3. Generate AI outreach drafts if not already populated
        if not job.outreach_note_short or not job.outreach_note_long:
            short_note, long_note = ai_service.generate_outreach_templates(
                job.company_name, job.job_title, job.job_description, job.recruiter_name
            )
            job.outreach_note_short = short_note
            job.outreach_note_long = long_note
            
        db.commit()
        
        # 4. Trigger SMTP auto-email outreach if email was guessed
        if job.recruiter_email and not job.email_sent:
            subject = f"Applied Machine Learning Scientist Application - Deshraj Jogiya"
            body = job.outreach_note_long or f"Dear {job.recruiter_name or 'Hiring Manager'},\n\nI recently submitted my application for the {job.job_title} role at {job.company_name}.\n\nWith my background in machine learning and data pipeline orchestration, I would love to connect. Please find my portfolio at https://deshraj-jogiya.github.io/\n\nSincerely,\nDeshraj Jogiya"
            
            success = send_outreach_email(job.recruiter_email, subject, body)
            if success:
                job.email_sent = True
                db.commit()
                
    except Exception as e:
        print(f"Error in recruiter sourcing and outreach workflow: {e}")
    finally:
        db.close()
