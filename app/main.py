import os
import json
import threading
import imaplib
from datetime import datetime
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from .models import JobApplication, TailoredDocument
from .services import ai_service, autofill_service
from .services import scheduler as bg_scheduler

# Initialize Database tables
Base.metadata.create_all(bind=engine)

app = FastAPI(title="Job Search CRM & AI Application Tailoring Command Center")

# Static files and templates
os.makedirs("app/static", exist_ok=True)
os.makedirs("app/static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")

@app.on_event("startup")
def startup_event():
    # Start background scheduler for crawling new jobs
    bg_scheduler.start_scheduler()

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
        "Applied": [j for j in jobs if j.status == "Applied"],
        "Interviewing": [j for j in jobs if j.status == "Interviewing"],
        "Offer": [j for j in jobs if j.status == "Offer"],
        "Rejected": [j for j in jobs if j.status == "Rejected"]
    }
    
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "jobs": jobs,
            "total": total,
            "applied": applied,
            "avg_score": avg_score,
            "conversion_rate": conversion_rate,
            "offers": offers,
            "kanban": kanban
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
        status="Ingested",
        recruiter_name=recruiter_name,
        recruiter_linkedin=recruiter_linkedin,
        outreach_note_short=short_note,
        outreach_note_long=long_note
    )
    db.add(job_app)
    db.commit()
    db.refresh(job_app)
    
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
        "job_detail.html",
        {
            "request": request,
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
        "resume_print.html",
        {
            "request": request,
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
        "cover_letter_print.html",
        {
            "request": request,
            "paragraphs": paragraphs,
            "resume": resume_data,
            "job": job,
            "date_today": datetime.utcnow().strftime("%B %d, %Y")
        }
    )

@app.get("/settings", response_class=HTMLResponse)
def get_settings_page(request: Request):
    load_dotenv()
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "imap_host": os.getenv("IMAP_HOST", "imap.gmail.com"),
            "imap_user": os.getenv("IMAP_USER", ""),
            "imap_password": os.getenv("IMAP_PASSWORD", ""),
            "success": None,
            "message": None
        }
    )

@app.post("/settings/update", response_class=HTMLResponse)
def update_settings(
    request: Request,
    imap_host: str = Form(...),
    imap_user: str = Form(...),
    imap_password: str = Form(...)
):
    # Update .env file
    env_path = ".env"
    env_lines = []
    
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            env_lines = f.readlines()
            
    # Parse existing variables
    env_dict = {}
    for line in env_lines:
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            env_dict[k.strip()] = v.strip()
            
    # Update properties
    env_dict["IMAP_HOST"] = imap_host
    env_dict["IMAP_USER"] = imap_user
    env_dict["IMAP_PASSWORD"] = imap_password
    
    # Rewrite file
    with open(env_path, "w", encoding="utf-8") as f:
        written_keys = set()
        for line in env_lines:
            if "=" in line and not line.strip().startswith("#"):
                k, _ = line.split("=", 1)
                k_clean = k.strip()
                if k_clean in env_dict:
                    f.write(f"{k_clean}={env_dict[k_clean]}\n")
                    written_keys.add(k_clean)
                else:
                    f.write(line)
            else:
                f.write(line)
                
        for k_clean, val in env_dict.items():
            if k_clean not in written_keys:
                f.write(f"{k_clean}={val}\n")
                
    load_dotenv()
    
    success = False
    message = ""
    try:
        print(f"Testing IMAP connection for {imap_user} at {imap_host}...")
        mail = imaplib.IMAP4_SSL(imap_host, 993)
        mail.login(imap_user, imap_password)
        mail.logout()
        success = True
        message = "Connection successful! Email monitoring credentials are valid and active."
    except Exception as e:
        success = False
        message = f"Connection failed: {e}. Please check your credentials and make sure App Passwords are enabled."
        
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "imap_host": imap_host,
            "imap_user": imap_user,
            "imap_password": imap_password,
            "success": success,
            "message": message
        }
    )
