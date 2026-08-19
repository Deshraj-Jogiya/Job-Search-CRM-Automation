import os
import secrets
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from urllib.parse import quote

from fastapi import FastAPI, Depends, Form, Request, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from .database import engine, Base, get_db
from .models import GlobalSettings, get_or_create_settings, JobApplication, ProfileVariant
from .csrf import CSRFMiddleware
from .templating import render
from .routers import profile as profile_router
from .routers import jobs as jobs_router
from .routers import confirmation as confirmation_router
from .routers import outreach as outreach_router
from .routers import analytics as analytics_router
from .services import backup_service
from .services import scheduler as bg_scheduler

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

app = FastAPI(title="Career Pilot -- Job Search Command Center")
app.add_middleware(CSRFMiddleware)

os.makedirs("app/static/css", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(profile_router.router, dependencies=app_dependencies)
app.include_router(jobs_router.router, dependencies=app_dependencies)
# NOT behind HTTPBasic: these routes are reached from a one-click email
# link opened on whatever device the user has in hand (see confirmation.py's
# docstring and confirmation_tokens.py) -- they carry their own signed
# bearer token instead, same reasoning as csrf.py's EXEMPT_PATH_PREFIXES.
app.include_router(confirmation_router.router)
app.include_router(outreach_router.router, dependencies=app_dependencies)
app.include_router(analytics_router.router, dependencies=app_dependencies)


@app.on_event("startup")
def on_startup():
    bg_scheduler.start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    bg_scheduler.stop_scheduler()


@app.get("/api/health", dependencies=app_dependencies)
def health_check():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


@app.get("/", response_class=HTMLResponse, dependencies=app_dependencies)
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
            "backup_configured": backup_service.is_configured(),
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@app.get("/settings/backup/export", dependencies=app_dependencies)
def export_backup(db: Session = Depends(get_db)):
    """Encrypted, on-demand DB export -- see backup_service's module
    docstring for why restore is deliberately not built alongside this."""
    try:
        encrypted_bytes, filename = backup_service.create_encrypted_backup()
    except RuntimeError as e:
        return RedirectResponse(url=f"/?error={quote(str(e))}", status_code=303)

    return Response(
        content=encrypted_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/settings/automation/toggle", dependencies=app_dependencies)
def toggle_automation(db: Session = Depends(get_db)):
    """Global kill switch -- halts crawling, tailoring, auto-apply, and
    outreach the moment it's flipped off. Every background job checks
    this fresh from the DB before doing real work."""
    settings = get_or_create_settings(db)
    settings.automation_enabled = not settings.automation_enabled
    db.commit()
    return RedirectResponse(url="/", status_code=303)


@app.post("/settings/update", dependencies=app_dependencies)
def update_settings(
    fast_poll_interval_minutes: int = Form(...),
    full_ingest_interval_minutes: int = Form(...),
    stale_posting_threshold_days: int = Form(...),
    confirmation_window_hours: float = Form(...),
    fast_track_score_threshold: int = Form(...),
    fast_track_freshness_minutes: int = Form(...),
    fast_track_window_hours: float = Form(...),
    rejected_retention_days: int = Form(...),
    daily_outreach_cap: int = Form(...),
    quiet_hours_enabled: bool = Form(False),
    quiet_hours_start_hour: int = Form(...),
    quiet_hours_end_hour: int = Form(...),
    local_timezone: str = Form(...),
    notification_digest_interval_minutes: int = Form(...),
    db: Session = Depends(get_db),
):
    """Every tunable number in the product is editable here -- nothing
    from our design discussion is hardcoded into the app itself."""
    settings = get_or_create_settings(db)
    settings.fast_poll_interval_minutes = fast_poll_interval_minutes
    settings.full_ingest_interval_minutes = full_ingest_interval_minutes
    settings.stale_posting_threshold_days = stale_posting_threshold_days
    settings.confirmation_window_hours = confirmation_window_hours
    settings.fast_track_score_threshold = fast_track_score_threshold
    settings.fast_track_freshness_minutes = fast_track_freshness_minutes
    settings.fast_track_window_hours = fast_track_window_hours
    settings.rejected_retention_days = rejected_retention_days
    settings.notification_digest_interval_minutes = notification_digest_interval_minutes
    settings.daily_outreach_cap = daily_outreach_cap
    settings.quiet_hours_enabled = quiet_hours_enabled
    settings.quiet_hours_start_hour = quiet_hours_start_hour
    settings.quiet_hours_end_hour = quiet_hours_end_hour
    settings.local_timezone = local_timezone.strip()
    db.commit()
    return RedirectResponse(url="/", status_code=303)
