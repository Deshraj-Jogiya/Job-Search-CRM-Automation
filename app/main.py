import os
import json
import secrets
import threading
import imaplib
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import text, func
from sqlalchemy.orm import Session

from .database import engine, Base, get_db, SessionLocal
from .models import JobApplication, TailoredDocument, SearchKeyword, ActivityLog
from .services import ai_service, autofill_service
from .services import scheduler as bg_scheduler
from .services.activity_logger import log_activity

# Initialize Database tables
Base.metadata.create_all(bind=engine)

# DB Migrations for SQLite
def run_migrations():
    from sqlalchemy import inspect
    inspector = inspect(engine)
    columns = [c['name'] for c in inspector.get_columns('job_applications')]
    
    with engine.begin() as conn:
        if 'recruiter_email' not in columns:
            try:
                conn.execute(text("ALTER TABLE job_applications ADD COLUMN recruiter_email VARCHAR;"))
            except Exception:
                pass
        if 'email_sent' not in columns:
            try:
                conn.execute(text("ALTER TABLE job_applications ADD COLUMN email_sent BOOLEAN DEFAULT 0;"))
            except Exception:
                pass
        if 'visa_sponsorship' not in columns:
            try:
                conn.execute(text("ALTER TABLE job_applications ADD COLUMN visa_sponsorship VARCHAR DEFAULT 'Unknown';"))
            except Exception:
                pass

run_migrations()

security_basic = HTTPBasic()

def verify_credentials(request: Request, credentials: HTTPBasicCredentials = Depends(security_basic)):
    # Bypass check for internal localhost requests (e.g. Playwright compiling PDFs locally)
    client_host = request.client.host if request.client else ""
    if client_host in ["127.0.0.1", "localhost", "::1"]:
        return "localhost"

    dashboard_password = os.getenv("DASHBOARD_PASSWORD")
    if not dashboard_password:
        return "none"
        
    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, dashboard_password)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized access",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Conditionally protect all routes if DASHBOARD_PASSWORD is set in environment
app_dependencies = []
if os.getenv("DASHBOARD_PASSWORD"):
    app_dependencies.append(Depends(verify_credentials))

app = FastAPI(
    title="Job Search CRM & AI Application Tailoring Command Center",
    dependencies=app_dependencies
)

# Static files and templates
os.makedirs("app/static", exist_ok=True)
os.makedirs("app/static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup_event():
    # Start background scheduler for crawling new jobs
    bg_scheduler.start_scheduler()
    
    # Conditional startup clean to wipe the 49 stale rejected jobs for development
    db = SessionLocal()
    try:
        rejected_count = db.query(JobApplication).filter(JobApplication.status == "Rejected").count()
        if rejected_count > 15:
            print("Wiping stale rejected jobs list for clean development slate...")
            db.query(TailoredDocument).delete()
            db.query(JobApplication).delete()
            db.query(ActivityLog).delete()
            db.commit()
    except Exception as e:
        print(f"Error in startup clean check: {e}")
    finally:
        db.close()

@app.on_event("shutdown")
def shutdown_event():
    # Stop background scheduler
    bg_scheduler.stop_scheduler()

def get_base_resume():
    """Helper to load the default base resume data."""
    base_resume_path = os.path.join(os.path.dirname(__file__), "base_resume.json")
    if os.path.exists(base_resume_path):
        with open(base_resume_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

@app.get("/", response_class=HTMLResponse)
def index(request: Request, db: Session = Depends(get_db)):
    # Fetch all job applications
    jobs = db.query(JobApplication).order_by(JobApplication.created_at.desc()).all()
    
    # Calculate quick metrics
    total = len(jobs)
    applied = sum(1 for j in jobs if j.status in ["Applied", "Interviewing", "Offer"])
    interviewing = sum(1 for j in jobs if j.status == "Interviewing")
    offers = sum(1 for j in jobs if j.status == "Offer")
    
    scores = [j.match_score for j in jobs if j.match_score > 0]
    avg_score = round(sum(scores) / len(scores)) if scores else 0
    
    conversion_rate = round((interviewing / applied) * 100) if applied else 0
    
    # Group jobs for the Kanban columns
    kanban = {
        "Ingested": [j for j in jobs if j.status == "Ingested"],
        "Tailored": [j for j in jobs if j.status == "Tailored"],
        "Needs Review": [j for j in jobs if j.status == "Needs Review"],
        "Applied": [j for j in jobs if j.status == "Applied"],
        "Interviewing": [j for j in jobs if j.status == "Interviewing"],
        "Offer": [j for j in jobs if j.status == "Offer"],
        "Rejected": [j for j in jobs if j.status == "Rejected"]
    }
    
    # Fetch search keywords
    keywords = db.query(SearchKeyword).all()
    
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "jobs": jobs,
            "total": total,
            "applied": applied,
            "avg_score": avg_score,
            "conversion_rate": conversion_rate,
            "offers": offers,
            "kanban": kanban,
            "keywords": keywords
        }
    )

@app.post("/jobs/ingest")
def ingest_job(
    company_name: str = Form(...),
    job_title: str = Form(...),
    job_url: str = Form(None),
    job_description: str = Form(...),
    recruiter_name: str = Form(None),
    recruiter_linkedin: str = Form(None),
    db: Session = Depends(get_db)
):
    # Check duplicate (case-insensitive and trimmed)
    exists = db.query(JobApplication).filter(
        (func.lower(JobApplication.company_name) == company_name.lower().strip()) &
        (func.lower(JobApplication.job_title) == job_title.lower().strip())
    ).first()
    if exists:
        log_activity(db, f"Skipped duplicate manual ingestion: {job_title} at {company_name}", "INFO")
        return RedirectResponse(url="/", status_code=303)

    # Retrieve base resume
    resume_data = get_base_resume()
    
    # Run AI evaluation & generate outreach drafts
    match_data = ai_service.evaluate_match(resume_data, job_description)
    short_note, long_note = ai_service.generate_outreach_templates(
        company_name, job_title, job_description, recruiter_name
    )
    
    # Create application record
    job_app = JobApplication(
        company_name=company_name,
        job_title=job_title,
        job_url=job_url,
        job_description=job_description,
        match_score=match_data.get("match_score", 50),
        match_analysis=json.dumps(match_data),
        visa_sponsorship=match_data.get("visa_sponsorship", "Unknown"),
        status="Ingested",
        recruiter_name=recruiter_name,
        recruiter_linkedin=recruiter_linkedin,
        outreach_note_short=short_note,
        outreach_note_long=long_note
    )
    db.add(job_app)
    db.commit()
    db.refresh(job_app)
    
    # Trigger instant tailoring and auto-apply pipeline in background
    threading.Thread(
        target=bg_scheduler.run_instant_pipeline_for_job,
        args=(job_app.id,),
        daemon=True
    ).start()
    
    return RedirectResponse(url="/", status_code=303)

@app.post("/jobs/crawl")
def trigger_manual_crawl():
    """Manually trigger job search, auto-apply, and email updates loop."""
    threading.Thread(
        target=bg_scheduler.trigger_crawling_and_apply_job,
        daemon=True
    ).start()
    return RedirectResponse(url="/", status_code=303)

@app.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job:
        raise HTTPException(status_code=4404, detail="Job not found")
        
    analysis = {}
    if job.match_analysis:
        try:
            analysis = json.loads(job.match_analysis)
        except Exception:
            pass
            
    # Load tailored documents
    resume_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id, 
        TailoredDocument.document_type == "resume"
    ).first()
    
    cl_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id, 
        TailoredDocument.document_type == "cover_letter"
    ).first()
    
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {
            "job": job,
            "analysis": analysis,
            "has_tailored_resume": resume_doc is not None,
            "has_tailored_cl": cl_doc is not None,
            "tailored_resume": resume_doc,
            "tailored_cl": cl_doc
        }
    )

@app.post("/jobs/{job_id}/tailor")
def tailor_application(job_id: int, db: Session = Depends(get_db)):
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    resume_data = get_base_resume()
    
    # 1. Tailor experience bullets (using multi-pass 95+ ATS optimizer)
    tailored_experience = ai_service.tailor_resume(resume_data, job.job_description)
    
    # Compile a tailored copy of the resume profile JSON
    tailored_resume_json = resume_data.copy()
    tailored_resume_json["experience"] = tailored_experience
    
    # Save or update tailored resume document
    resume_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id, 
        TailoredDocument.document_type == "resume"
    ).first()
    
    if not resume_doc:
        resume_doc = TailoredDocument(
            job_id=job_id,
            document_type="resume",
            content=json.dumps(tailored_resume_json)
        )
        db.add(resume_doc)
    else:
        resume_doc.content = json.dumps(tailored_resume_json)
        resume_doc.generated_at = datetime.utcnow()
        
    # 2. Tailor cover letter
    cl_text = ai_service.generate_cover_letter(
        resume_data, job.company_name, job.job_title, job.job_description
    )
    
    cl_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id, 
        TailoredDocument.document_type == "cover_letter"
    ).first()
    
    if not cl_doc:
        cl_doc = TailoredDocument(
            job_id=job_id,
            document_type="cover_letter",
            content=cl_text
        )
        db.add(cl_doc)
    else:
        cl_doc.content = cl_text
        cl_doc.generated_at = datetime.utcnow()
        
    # Update job status to Tailored
    job.status = "Tailored"
    db.commit()
    
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

@app.post("/jobs/{job_id}/autofill")
def trigger_autofill(job_id: int):
    """Run Playwright headed browser form filler in a background thread."""
    threading.Thread(
        target=autofill_service.autofill_job_application,
        args=(job_id,),
        daemon=True
    ).start()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

@app.post("/jobs/{job_id}/update-status")
def update_status(job_id: int, status: str = Form(...), db: Session = Depends(get_db)):
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    job.status = status
    if status == "Applied":
        job.applied_at = datetime.utcnow()
        db.commit()
        
        # Trigger recruiter sourcing and outreach
        from .services import networking_service
        threading.Thread(
            target=networking_service.trigger_recruiter_sourcing_and_outreach,
            args=(job_id,),
            daemon=True
        ).start()
    else:
        db.commit()
    
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

@app.post("/jobs/{job_id}/send-manual-email")
def send_manual_email(job_id: int, email_addr: str = Form(...), db: Session = Depends(get_db)):
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    from .services import networking_service
    subject = f"Applied Machine Learning Scientist Application - Deshraj Jogiya"
    body = job.outreach_note_long or f"Dear Hiring Team,\\n\\nI recently applied for the {job.job_title} role at {job.company_name}. I wanted to briefly connect and share my portfolio..."
    
    success = networking_service.send_outreach_email(email_addr, subject, body)
    if success:
        job.recruiter_email = email_addr
        job.email_sent = True
        db.commit()
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

@app.post("/jobs/{job_id}/update-notes")
def update_notes(job_id: int, notes: str = Form(...), db: Session = Depends(get_db)):
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job:
        raise HTTPException(status_code=4404, detail="Job not found")
        
    job.notes = notes
    db.commit()
    
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

@app.post("/jobs/{job_id}/delete")
def delete_job(job_id: int, db: Session = Depends(get_db)):
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    db.delete(job)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/jobs/reset-database")
def reset_database(db: Session = Depends(get_db)):
    """Deletes all job application and tailored document records to clean the slate."""
    db.query(TailoredDocument).delete()
    db.query(JobApplication).delete()
    log_activity(db, "Pipeline database was reset. Slate is clean.", "WARNING")
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/resumes/render/{job_id}", response_class=HTMLResponse)
def render_tailored_resume(job_id: int, request: Request, db: Session = Depends(get_db)):
    resume_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id, 
        TailoredDocument.document_type == "resume"
    ).first()
    
    if not resume_doc:
        resume_data = get_base_resume()
    else:
        resume_data = json.loads(resume_doc.content)
        
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    
    return templates.TemplateResponse(
        request,
        "resume_print.html",
        {
            "resume": resume_data,
            "job": job
        }
    )

@app.get("/cover-letters/render/{job_id}", response_class=HTMLResponse)
def render_tailored_cover_letter(job_id: int, request: Request, db: Session = Depends(get_db)):
    cl_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id, 
        TailoredDocument.document_type == "cover_letter"
    ).first()
    
    if not cl_doc:
        raise HTTPException(status_code=404, detail="Cover letter not generated yet.")
        
    resume_data = get_base_resume()
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    paragraphs = cl_doc.content.split("\n\n")
    
    return templates.TemplateResponse(
        request,
        "cover_letter_print.html",
        {
            "paragraphs": paragraphs,
            "resume": resume_data,
            "job": job,
            "date_today": datetime.utcnow().strftime("%B %d, %Y")
        }
    )

@app.get("/api/logs")
def get_activity_logs(db: Session = Depends(get_db)):
    from .models import ActivityLog
    logs = db.query(ActivityLog).order_by(ActivityLog.timestamp.desc()).limit(45).all()
    return [{"message": l.message, "level": l.level, "timestamp": l.timestamp.strftime("%Y-%m-%d %H:%M:%S")} for l in logs]

@app.get("/api/queries")
def get_search_queries(db: Session = Depends(get_db)):
    from .models import SearchKeyword
    queries = db.query(SearchKeyword).all()
    return [{"id": q.id, "keyword": q.keyword, "is_active": q.is_active} for q in queries]

@app.post("/api/queries")
def add_search_query(keyword: str = Form(...), db: Session = Depends(get_db)):
    from .models import SearchKeyword
    keyword_clean = keyword.strip()
    if not keyword_clean:
        raise HTTPException(status_code=400, detail="Keyword cannot be empty")
    
    # Check duplicate
    exists = db.query(SearchKeyword).filter(SearchKeyword.keyword == keyword_clean).first()
    if exists:
        return RedirectResponse(url="/", status_code=303)
        
    q = SearchKeyword(keyword=keyword_clean, is_active=True)
    db.add(q)
    db.commit()
    
    log_activity(db, f"Added search query keyword: {keyword_clean}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/queries/{query_id}/delete")
def delete_search_query(query_id: int, db: Session = Depends(get_db)):
    from .models import SearchKeyword
    q = db.query(SearchKeyword).filter(SearchKeyword.id == query_id).first()
    if q:
        kw = q.keyword
        db.delete(q)
        db.commit()
        log_activity(db, f"Deleted search query keyword: {kw}")
    return RedirectResponse(url="/", status_code=303)

@app.post("/api/queries/{query_id}/toggle")
def toggle_search_query(query_id: int, db: Session = Depends(get_db)):
    from .models import SearchKeyword
    q = db.query(SearchKeyword).filter(SearchKeyword.id == query_id).first()
    if q:
        q.is_active = not q.is_active
        db.commit()
        log_activity(db, f"Toggled keyword '{q.keyword}' to {'Active' if q.is_active else 'Inactive'}")
    return RedirectResponse(url="/", status_code=303)

