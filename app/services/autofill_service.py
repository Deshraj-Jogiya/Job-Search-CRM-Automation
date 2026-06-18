import os
import json
import time
from playwright.sync_api import sync_playwright
from sqlalchemy.orm import Session
from ..database import SessionLocal
from ..models import JobApplication, TailoredDocument, CandidateAccount

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
        page.wait_for_timeout(1000)
        
        page.pdf(path=pdf_path, format="A4", print_background=True)
        browser.close()
        
    print(f"Tailored PDF compiled at: {pdf_path}")
    return pdf_path

def handle_workday_signup(page, email, password):
    """Automate sign-up screen navigation on Workday portal."""
    print("Navigating Workday registration flow...")
    try:
        # Look for "Create Account" or "Sign Up" links
        create_acc_selectors = [
            "a[href*='register']", "button[data-automation-id='createAccountButton']", 
            "a[data-automation-id='createAccountLink']", "text=Create Account", "text=Sign Up"
        ]
        
        for sel in create_acc_selectors:
            if page.locator(sel).count() > 0:
                page.locator(sel).first.click()
                page.wait_for_timeout(2000)
                break
                
        # Fill signup fields
        # Workday signup fields typically use:
        # emailAddress, password, confirmPassword
        if page.locator("input[type='email']").count() > 0:
            page.locator("input[type='email']").first.fill(email)
            
        pass_inputs = page.locator("input[type='password']").all()
        if len(pass_inputs) >= 2:
            pass_inputs[0].fill(password)
            pass_inputs[1].fill(password)
            
        # Agreement checkbox
        chk = page.locator("input[type='checkbox']")
        if chk.count() > 0:
            chk.first.check()
            
        # Click submit
        submit_btn = page.locator("button[type='submit'], button:has-text('Create Account'), button:has-text('Register')")
        if submit_btn.count() > 0:
            submit_btn.first.click()
            page.wait_for_timeout(4000)
            
            # Check for CAPTCHA
            if page.locator("iframe[src*='recaptcha'], iframe[src*='turnstile']").count() > 0:
                print("⚠️ CAPTCHA DETECTED! Pausing automation. Please solve it on-screen to proceed.")
                # We wait for the URL to change (meaning captcha was solved and user logged in)
                page.wait_for_url("**/jobs/**", timeout=120000)
                
            return True
    except Exception as e:
        print(f"Workday signup automation warning: {e}")
    return False

def autofill_job_application(job_id: int, auto_submit: bool = False):
    """Launch headed browser, create candidate account if needed (Workday), autofill form, and optionally submit."""
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
        
        cl_text = ai_service.generate_cover_letter(base_resume, job.company_name, job.job_title, job.job_description)
        cl_doc = TailoredDocument(job_id=job_id, document_type="cover_letter", content=cl_text)
        db.add(cl_doc)
        
        job.status = "Tailored"
        db.commit()
    
    resume_data = json.loads(resume_doc.content)
    
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
    
    # Default portal credentials
    default_password = "DeshrajApply2026!"
    
    # Check if account is already logged in the DB
    account = db.query(CandidateAccount).filter_by(company_name=job.company_name).first()
    
    print(f"Launching headed Chromium browser for application: {job.job_url}")
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        context = browser.new_context(viewport=None)
        page = context.new_page()
        page.goto(job.job_url)
        
        # Helper to fill elements
        def fill_if_exists(selector, value):
            try:
                if page.locator(selector).count() > 0:
                    page.locator(selector).first.fill(value)
            except Exception:
                pass
                
        page.wait_for_timeout(3000)
        
        # A. WORKDAY PORTAL FLOW
        if "myworkdayjobs.com" in job.job_url:
            print("Workday Portal detected. Checking signup/login screen...")
            
            # Click "Apply" first if we are on the landing page
            apply_btns = ["a:has-text('Apply')", "button:has-text('Apply')", "a[data-automation-id='adventureButton']"]
            for btn in apply_btns:
                if page.locator(btn).count() > 0:
                    page.locator(btn).first.click()
                    page.wait_for_timeout(3000)
                    break
            
            # Check if login/sign-in screen is visible
            if page.locator("input[type='email']").count() > 0 or page.locator("button[data-automation-id='signInButton']").count() > 0:
                if not account:
                    # Create account
                    handle_workday_signup(page, email, default_password)
                    
                    # Log credentials to DB
                    account = CandidateAccount(
                        company_name=job.company_name,
                        login_url=job.job_url,
                        username=email,
                        password=default_password
                    )
                    db.add(account)
                    db.commit()
                    db.refresh(account)
                    job.account_id = account.id
                    db.commit()
                else:
                    # Log in using existing credentials
                    print("Signing in with existing logged credentials...")
                    fill_if_exists("input[type='email']", account.username)
                    fill_if_exists("input[type='password']", account.password)
                    
                    # Submit login
                    signin_btn = page.locator("button[type='submit'], button[data-automation-id='signInButton']")
                    if signin_btn.count() > 0:
                        signin_btn.first.click()
                        page.wait_for_timeout(3000)
            
            # Run autofill actions inside Workday form
            print("Running Workday form inputs...")
            try:
                # Look for "Apply Manually" button
                apply_man = page.locator("button[data-automation-id='applyManuallyButton'], text=Apply Manually")
                if apply_man.count() > 0:
                    apply_man.first.click()
                    page.wait_for_timeout(3000)
                
                # Fill contact details page
                fill_if_exists("input[data-automation-id='legalNameSection_firstName']", first_name)
                fill_if_exists("input[data-automation-id='legalNameSection_lastName']", last_name)
                fill_if_exists("input[data-automation-id='phone-number']", phone)
                
                # Address fallbacks
                fill_if_exists("input[data-automation-id='addressSection_addressLine1']", "123 University Drive")
                fill_if_exists("input[data-automation-id='addressSection_city']", "Tempe")
                fill_if_exists("input[data-automation-id='addressSection_postalCode']", "85281")
                
                # Click next
                next_btn = page.locator("button[data-automation-id='bottom-navigation-next-button']")
                if next_btn.count() > 0:
                    next_btn.first.click()
                    page.wait_for_timeout(2000)
            except Exception as e:
                print(f"Workday form fill warning: {e}")
                
        # B. GREENHOUSE FORM FLOW
        elif "greenhouse" in job.job_url or page.locator("#first_name").count() > 0:
            print("Autofilling Greenhouse Form...")
            fill_if_exists("#first_name", first_name)
            fill_if_exists("#last_name", last_name)
            fill_if_exists("#email", email)
            fill_if_exists("#phone", phone)
            
            if page.locator("input[type='file'][id='resume_file']").count() > 0:
                page.locator("input[type='file'][id='resume_file']").set_input_files(pdf_path)
                print("Attached Resume PDF.")
                
            # Match links
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
            except Exception:
                pass
                
            if auto_submit:
                submit_btn = page.locator("#submit_app")
                if submit_btn.count() > 0:
                    submit_btn.first.click()
                    job.status = "Applied"
                    job.applied_at = datetime.utcnow()
                    db.commit()
                    print("Form submitted successfully (Greenhouse).")
                    
        # C. LEVER FORM FLOW
        elif "lever.co" in job.job_url or page.locator("input[name='name']").count() > 0:
            print("Autofilling Lever Form...")
            fill_if_exists("input[name='name']", full_name)
            fill_if_exists("input[name='email']", email)
            fill_if_exists("input[name='phone']", phone)
            fill_if_exists("input[name='org']", "Objectways Technologies")
            
            if page.locator("input[type='file'][class*='resume']").count() > 0:
                page.locator("input[type='file'][class*='resume']").set_input_files(pdf_path)
            elif page.locator("#resume-upload-input").count() > 0:
                page.locator("#resume-upload-input").set_input_files(pdf_path)
                
            fill_if_exists("input[name='urls[LinkedIn]']", linkedin)
            fill_if_exists("input[name='urls[GitHub]']", github)
            
            if auto_submit:
                submit_btn = page.locator("button[type='submit']")
                if submit_btn.count() > 0:
                    submit_btn.first.click()
                    job.status = "Applied"
                    job.applied_at = datetime.utcnow()
                    db.commit()
                    print("Form submitted successfully (Lever).")
                    
        # D. OTHER FORMS
        else:
            print("Autofilling generic inputs...")
            fill_if_exists("input[name*='name'], input[id*='name']", full_name)
            fill_if_exists("input[type='email'], input[name*='email'], input[id*='email']", email)
            fill_if_exists("input[type='tel'], input[name*='phone'], input[id*='phone']", phone)
            
        # If auto_submit is false or it's a workday/custom portal, keep browser open for user review
        print("\n--- AUTOFILLED DONE ---")
        print("Please review and complete application fields.")
        
        while True:
            try:
                if page.is_closed():
                    break
                page.wait_for_timeout(2000)
            except Exception:
                break
                
    # Clean up temp file
    try:
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
            print("Cleaned up temporary PDF.")
    except Exception:
        pass
        
    db.close()
    return True
