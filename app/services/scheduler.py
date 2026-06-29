import os
import json
import threading
from apscheduler.schedulers.background import BackgroundScheduler
from ..database import SessionLocal
from . import crawler, autofill_service, email_monitor, ai_service, networking_service
from .activity_logger import log_activity
from ..models import JobApplication, TailoredDocument

scheduler = BackgroundScheduler()

def get_base_resume():
    """Load the candidate profile details from json."""
    base_resume_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "base_resume.json")
    if os.path.exists(base_resume_path):
        with open(base_resume_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def run_instant_pipeline_for_job(job_id: int):
    """End-to-end background pipeline for a single ingested job:
    Sourcing Recruiter -> AI Resume/CL Tailoring -> Playwright Autofill Application.
    """
    db = SessionLocal()
    try:
        job = db.query(JobApplication).filter_by(id=job_id).first()
        if not job:
            return
            
        log_activity(db, f"Starting automation pipeline for: {job.job_title} at {job.company_name}", "INFO")
        
        # 1. Recruiter Sourcing
        if not job.recruiter_name or not job.recruiter_email:
            recruiter_name, recruiter_url = networking_service.search_recruiter(job.company_name)
            if recruiter_name:
                job.recruiter_name = recruiter_name
                job.recruiter_linkedin = recruiter_url
                email_guess = networking_service.guess_recruiter_email(recruiter_name, job.company_name)
                job.recruiter_email = email_guess
                log_activity(db, f"Sourced recruiter: {recruiter_name} ({email_guess})", "INFO")
                db.commit()
                
        # 2. AI Resume & Cover Letter Tailoring
        existing_res = db.query(TailoredDocument).filter_by(job_id=job.id, document_type="resume").first()
        if not existing_res:
            resume_data = get_base_resume()
            log_activity(db, f"Tailoring resume experiences for {job.company_name}...", "INFO")
            tailored_experience = ai_service.tailor_resume(resume_data, job.job_description)
            
            tailored_resume_json = resume_data.copy()
            tailored_resume_json["experience"] = tailored_experience
            
            res_doc = TailoredDocument(
                job_id=job.id,
                document_type="resume",
                content=json.dumps(tailored_resume_json)
            )
            db.add(res_doc)
            
            log_activity(db, f"Generating custom cover letter for {job.company_name}...", "INFO")
            cl_text = ai_service.generate_cover_letter(resume_data, job.company_name, job.job_title, job.job_description)
            cl_doc = TailoredDocument(
                job_id=job.id,
                document_type="cover_letter",
                content=cl_text
            )
            db.add(cl_doc)
            
            job.status = "Tailored"
            db.commit()
            log_activity(db, f"Tailored documents successfully generated.", "INFO")
            
        # 3. Playwright Autofill Auto-Apply
        if job.match_score >= 65:
            log_activity(db, f"High match ({job.match_score}%). Triggering Playwright auto-apply...", "INFO")
            autofill_service.autofill_job_application(job.id, auto_submit=True)
        else:
            log_activity(db, f"Match score {job.match_score}% is below 65%. Keeping in queue for manual review.", "INFO")
            
    except Exception as e:
        log_activity(db, f"Error running automation pipeline for job {job_id}: {e}", "ERROR")
        print(f"Error in instant pipeline: {e}")
    finally:
        db.close()

def trigger_crawling_and_apply_job(timeframe: str = "24h"):
    """Trigger the public job crawler, auto-tailor, auto-apply, and run email inbox scan."""
    db = SessionLocal()
    resume_data = get_base_resume()
    try:
        # 1. Scrape new jobs with timeframe filter
        new_job_ids = crawler.run_daily_crawl_and_ingest(db, resume_data, timeframe=timeframe)
        
        # 2. Trigger instant pipeline sequentially for each new job to respect API rate limits
        import time
        for j_id in new_job_ids:
            try:
                run_instant_pipeline_for_job(j_id)
                time.sleep(3)
            except Exception as pe:
                print(f"Error executing sequential pipeline for job {j_id}: {pe}")
        
        # 2. Scan IMAP inbox for status updates (rejections, interviews)
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

def process_stuck_ingested_jobs():
    """Wakes up and processes any jobs currently stuck in the Ingested queue."""
    db = SessionLocal()
    try:
        stuck_jobs = db.query(JobApplication).filter_by(status="Ingested").all()
        if stuck_jobs:
            log_activity(db, f"Found {len(stuck_jobs)} stuck Ingested jobs. Launching background processor...", "WARNING")
            import time
            for job in stuck_jobs:
                try:
                    run_instant_pipeline_for_job(job.id)
                    time.sleep(3)
                except Exception as e:
                    log_activity(db, f"Error processing stuck job {job.id}: {e}", "ERROR")
                    print(f"Error processing stuck job {job.id}: {e}")
    except Exception as e:
        log_activity(db, f"Error in stuck jobs processor: {e}", "ERROR")
        print(f"Error in stuck jobs processor: {e}")
    finally:
        db.close()
