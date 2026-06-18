import os
import json
from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy.orm import sessionmaker
from ..database import engine, SessionLocal
from . import crawler

scheduler = BackgroundScheduler()

def get_base_resume():
    """Load the candidate profile details from json."""
    base_resume_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "base_resume.json")
    if os.path.exists(base_resume_path):
        with open(base_resume_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def trigger_crawling_job():
    """Trigger the public job crawler pipeline."""
    db = SessionLocal()
    resume_data = get_base_resume()
    try:
        crawler.run_daily_crawl_and_ingest(db, resume_data)
    except Exception as e:
        print(f"Error in scheduled crawling job: {e}")
    finally:
        db.close()

def start_scheduler():
    """Initialize and start background cron/interval job searches."""
    if not scheduler.running:
        # Run crawler once immediately at startup, then every 24 hours
        scheduler.add_job(trigger_crawling_job, trigger='interval', hours=24, name="daily_job_crawler")
        scheduler.start()
        print("Background job search scheduler has started successfully.")
        
        # Trigger an initial crawl in a non-blocking thread so server startup is fast
        import threading
        threading.Thread(target=trigger_crawling_job, daemon=True).start()

def stop_scheduler():
    if scheduler.running:
        scheduler.shutdown()
        print("Background job search scheduler has shutdown.")
