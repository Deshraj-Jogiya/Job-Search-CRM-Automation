import os
import json
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from ..database import SessionLocal
from . import crawler, autofill_service, email_monitor
from ..models import JobApplication

scheduler = BackgroundScheduler()

def get_base_resume():
    """Load the candidate profile details from json."""
    base_resume_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "base_resume.json")
    if os.path.exists(base_resume_path):
        with open(base_resume_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def trigger_crawling_and_apply_job():
    """Trigger the public job crawler, auto-tailor, auto-apply, and run email inbox scan."""
    db = SessionLocal()
    resume_data = get_base_resume()
    try:
        # 1. Scrape new jobs
        crawler.run_daily_crawl_and_ingest(db, resume_data)
        
        # 2. Check for high-matching ingested jobs to auto-tailor & autofill
        # Find jobs ingested in this run (Status: Ingested) with Score >= 85
        high_matches = db.query(JobApplication).filter(
            JobApplication.status == "Ingested",
            JobApplication.match_score >= 85
        ).all()
        
        for job in high_matches:
            print(f"🔥 Found high compatibility match: '{job.job_title}' at '{job.company_name}' ({job.match_score}%). Triggering instant auto-apply...")
            # Trigger Playwright autofill in a background thread so it doesn't block the scheduler loop
            # Sets auto_submit=True to automate Greenhouse/Lever submissions
            threading.Thread(
                target=autofill_service.autofill_job_application,
                args=(job.id, True),
                daemon=True
            ).start()
            
        # 3. Scan IMAP inbox for status updates (rejections, interviews)
        print("Starting scheduled email status updates check...")
        email_monitor.scan_inbox_for_updates()
        
    except Exception as e:
        print(f"Error in scheduled crawling and apply loop: {e}")
    finally:
        db.close()

def start_scheduler():
    """Initialize and start background cron/interval job searches."""
    if not scheduler.running:
        # Run active crawler & apply queue every 15 minutes
        scheduler.add_job(trigger_crawling_and_apply_job, trigger='interval', minutes=15, name="realtime_job_crawler")
        scheduler.start()
        print("Background real-time job crawler & apply scheduler has started.")
        
        # Trigger an initial crawl at startup
        threading.Thread(target=trigger_crawling_and_apply_job, daemon=True).start()

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("Background scheduler has shutdown.")
