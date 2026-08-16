import os
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv
from fastapi import FastAPI, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from .models import GlobalSettings, get_or_create_settings, JobApplication, ProfileVariant
from .csrf import CSRFMiddleware, get_csrf_token

load_dotenv()

Base.metadata.create_all(bind=engine)

security_basic = HTTPBasic()


def verify_credentials(request: Request, credentials: HTTPBasicCredentials = Depends(security_basic)):
    client_host = request.client.host if request.client else ""
    if client_host in ["127.0.0.1", "localhost", "::1"]:
        return "localhost"

    dashboard_password = os.getenv("DASHBOARD_PASSWORD")
    if not dashboard_password:
        return "none"

    correct_username = secrets.compare_digest(credentials.username, "admin")
    correct_password = secrets.compare_digest(credentials.password, dashboard_password)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def get_admin_password() -> str:
    pwd = os.getenv("ADMIN_PASSWORD")
    if not pwd:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured. Destructive actions are disabled.")
    return pwd


app_dependencies = []
if os.getenv("DASHBOARD_PASSWORD"):
    app_dependencies.append(Depends(verify_credentials))

app = FastAPI(title="Career Pilot -- Job Search Command Center", dependencies=app_dependencies)
app.add_middleware(CSRFMiddleware)

os.makedirs("app/static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")


def render(request: Request, template_name: str, context: dict):
    context["csrf_token"] = get_csrf_token(request)
    return templates.TemplateResponse(request, template_name, context)


@app.get("/api/health")
def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    total_applications = db.query(JobApplication).count()
    profile_variants = db.query(ProfileVariant).all()

    return render(
        request,
        "dashboard.html",
        {
            "settings": settings,
            "total_applications": total_applications,
            "profile_variants": profile_variants,
        },
    )


@app.post("/settings/automation/toggle")
def toggle_automation(db: Session = Depends(get_db)):
    """Global kill switch -- halts crawling, tailoring, auto-apply, and
    outreach the moment it's flipped off. Every background job checks
    this fresh from the DB before doing real work."""
    settings = get_or_create_settings(db)
    settings.automation_enabled = not settings.automation_enabled
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings/update")
def update_settings(
    fast_poll_interval_minutes: int = Form(...),
    full_ingest_interval_minutes: int = Form(...),
    confirmation_window_hours: float = Form(...),
    fast_track_score_threshold: int = Form(...),
    fast_track_freshness_minutes: int = Form(...),
    fast_track_window_hours: float = Form(...),
    rejected_retention_days: int = Form(...),
    daily_outreach_cap: int = Form(...),
    db: Session = Depends(get_db),
):
    """Every tunable number in the product is editable here -- nothing
    from our design discussion is hardcoded into the app itself."""
    settings = get_or_create_settings(db)
    settings.fast_poll_interval_minutes = fast_poll_interval_minutes
    settings.full_ingest_interval_minutes = full_ingest_interval_minutes
    settings.confirmation_window_hours = confirmation_window_hours
    settings.fast_track_score_threshold = fast_track_score_threshold
    settings.fast_track_freshness_minutes = fast_track_freshness_minutes
    settings.fast_track_window_hours = fast_track_window_hours
    settings.rejected_retention_days = rejected_retention_days
    settings.daily_outreach_cap = daily_outreach_cap
    db.commit()
    return RedirectResponse(url="/", status_code=303)
