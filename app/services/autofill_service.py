import os
import json
import time
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session
from ..database import SessionLocal
from ..models import JobApplication, TailoredDocument

def compile_resume_to_pdf(job_id: int) -> str:
    """Load the tailored HTML resume from the local web server and print it to a PDF file."""
    pdf_path = os.path.abspath(f"G:\\Job-Search-CRM-Automation\\tailored_resume_{job_id}.pdf")
    
    # Use Playwright to load local render page and print to PDF
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        
        # Load the HTML resume print view
        url = f"http://localhost:8000/resumes/render/{job_id}"
        print(f"Compiling PDF from: {url}")
        page.goto(url)
        # Wait a moment for any assets to load
        page.wait_for_timeout(1000)
        
        # Print to PDF using browser print options
        page.pdf(path=pdf_path, format="A4", print_background=True)
        browser.close()
        
    print(f"Tailored PDF compiled at: {pdf_path}")
    return pdf_path

def autofill_job_application(job_id: int):
    """Launch headed browser and autofill Lever/Greenhouse application forms."""
    db = SessionLocal()
    job = db.query(JobApplication).filter(JobApplication.id == job_id).first()
    if not job or not job.job_url:
        print("Job application URL not available.")
        db.close()
        return False
        
    # Get tailored resume content or fallback
    resume_doc = db.query(TailoredDocument).filter(
        TailoredDocument.job_id == job_id,
        TailoredDocument.document_type == "resume"
    ).first()
    
    if not resume_doc:
        print("Tailored resume not generated yet. Auto-tailoring now...")
        # Trigger tailoring
        base_resume_path = os.path.abspath("G:\\Job-Search-CRM-Automation\\app\\base_resume.json")
        with open(base_resume_path, "r", encoding="utf-8") as f:
            base_resume = json.load(f)
        from . import ai_service
        tailored_exp = ai_service.tailor_resume(base_resume, job.job_description)
        tailored_resume = base_resume.copy()
        tailored_resume["experience"] = tailored_exp
        
        resume_doc = TailoredDocument(
            job_id=job_id,
            document_type="resume",
            content=json.dumps(tailored_resume)
        )
        db.add(resume_doc)
        
        # Also generate cover letter
        cl_text = ai_service.generate_cover_letter(base_resume, job.company_name, job.job_title, job.job_description)
        cl_doc = TailoredDocument(job_id=job_id, document_type="cover_letter", content=cl_text)
        db.add(cl_doc)
        
        job.status = "Tailored"
        db.commit()
    
    resume_data = json.loads(resume_doc.content)
    db.close()
    
    # 1. Compile PDF
    pdf_path = compile_resume_to_pdf(job_id)
    
    # Contact information variables
    contact = resume_data.get("contact", {})
    full_name = resume_data.get("name", "")
    first_name = full_name.split(" ")[0] if " " in full_name else full_name
    last_name = full_name.split(" ", 1)[1] if " " in full_name else ""
    email = contact.get("email", "")
    phone = contact.get("phone", "")
    linkedin = contact.get("linkedin", "")
    github = contact.get("github", "")
    
    # 2. Launch headed Playwright browser so the user can see it and take over
    print(f"Launching headed Chromium browser for application: {job.job_url}")
    
    # Run playwright in headed mode
    with sync_playwright() as p:
        # Launch browser with local profile or standard parameters
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(viewport=None)
        page = context.new_page()
        page.goto(job.job_url)
        
        # Helper to wait for element and fill
        def fill_if_exists(selector, value):
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).fill(value)
                    print(f"Filled: {selector}")
            except Exception as e:
                print(f"Could not fill selector {selector}: {e}")
                
        # Wait for form to render
        page.wait_for_timeout(2000)
        
        # A. Detect Greenhouse Forms
        if "greenhouse" in job.job_url or page.locator("#first_name").count() > 0:
            print("Detecting Greenhouse Form layout...")
            fill_if_exists("#first_name", first_name)
            fill_if_exists("#last_name", last_name)
            fill_if_exists("#email", email)
            fill_if_exists("#phone", phone)
            
            # Attaching Resume
            try:
                # Greenhouse usually has button selector button[data-source="attach"]
                # which triggers file chooser
                if page.locator("input[type='file'][id='resume_file']").count() > 0:
                    page.locator("input[type='file'][id='resume_file']").set_input_files(pdf_path)
                    print("Attached Resume PDF.")
            except Exception as e:
                print("Error uploading resume file on Greenhouse:", e)
                
            # Fill links
            # Greenhouse matches by inputs containing text values
            try:
                inputs = page.locator("input[type='text']").all()
                for inp in inputs:
                    name_attr = inp.get_attribute("name") or ""
                    id_attr = inp.get_attribute("id") or ""
                    placeholder = inp.get_attribute("placeholder") or ""
                    
                    if "linkedin" in name_attr.lower() or "linkedin" in id_attr.lower() or "linkedin" in placeholder.lower():
                        inp.fill(linkedin)
                    elif "github" in name_attr.lower() or "github" in id_attr.lower() or "github" in placeholder.lower():
                        inp.fill(github)
            except Exception as e:
                print("Error scanning secondary input links:", e)
                
        # B. Detect Lever Forms
        elif "lever.co" in job.job_url or page.locator("input[name='name']").count() > 0:
            print("Detecting Lever Form layout...")
            fill_if_exists("input[name='name']", full_name)
            fill_if_exists("input[name='email']", email)
            fill_if_exists("input[name='phone']", phone)
            fill_if_exists("input[name='org']", "Objectways Technologies") # Default current company
            
            # Attaching Resume
            try:
                if page.locator("input[type='file'][class*='resume']").count() > 0:
                    page.locator("input[type='file'][class*='resume']").set_input_files(pdf_path)
                    print("Attached Resume PDF.")
                elif page.locator("#resume-upload-input").count() > 0:
                    page.locator("#resume-upload-input").set_input_files(pdf_path)
                    print("Attached Resume PDF.")
            except Exception as e:
                print("Error uploading resume file on Lever:", e)
                
            # Fill links
            fill_if_exists("input[name='urls[LinkedIn]']", linkedin)
            fill_if_exists("input[name='urls[GitHub]']", github)
            
        # C. Generic Form Fallbacks
        else:
            print("Running generic input scanners...")
            # Try to match inputs by label name
            fill_if_exists("input[name*='name'], input[id*='name']", full_name)
            fill_if_exists("input[type='email'], input[name*='email'], input[id*='email']", email)
            fill_if_exists("input[type='tel'], input[name*='phone'], input[id*='phone']", phone)
            
        print("\n--- AUTOFILLED DONE ---")
        print("Please check the browser window. Review the fields, complete any custom questions/CAPTCHAs, and click Submit manually.")
        
        # Keep the browser open indefinitely so the user can interact
        while True:
            try:
                # Keep active if browser is not closed
                if page.is_closed():
                    break
                page.wait_for_timeout(2000)
            except Exception:
                break
                
    # Delete the temp pdf file after browser closes to save space
    try:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
            print("Cleaned up temporary PDF.")
    except Exception as e:
        print("Error deleting PDF:", e)
        
    return True
