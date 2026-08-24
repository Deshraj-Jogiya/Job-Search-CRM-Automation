import os
import secrets
from dotenv import load_dotenv

load_dotenv()

from .logging_config import configure_logging

configure_logging()

from urllib.parse import quote

from fastapi import FastAPI, Depends, File, Form, Request, HTTPException, UploadFile
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from .app_mode import is_showcase_mode
from .database import engine, Base, SessionLocal, get_db, utcnow
from .models import AdminAccount, GlobalSettings, SearchKeyword, get_or_create_settings, JobApplication, ProfileVariant
from .csrf import CSRFMiddleware
from .templating import render
from .routers import auth as auth_router
from .routers import profile as profile_router
from .routers import jobs as jobs_router
from .routers import confirmation as confirmation_router
from .routers import outreach as outreach_router
from .routers import analytics as analytics_router
from .services import auth_service, backup_service, profile_service
from .services import scheduler as bg_scheduler
from .services.activity_logger import log_activity

Base.metadata.create_all(bind=engine)


def get_admin_password() -> str:
    pwd = os.getenv("ADMIN_PASSWORD")
    if not pwd:
        raise HTTPException(status_code=503, detail="ADMIN_PASSWORD is not configured. Destructive actions are disabled.")
    return pwd


class NotAuthenticated(Exception):
    """Raised by require_auth to send an unauthenticated browser request
    to the login page -- a plain HTTPException can't carry a redirect,
    so this gets its own exception handler below."""

    def __init__(self, next_path: str):
        self.next_path = next_path


def require_auth(request: Request, db: Session = Depends(get_db)):
    """Real per-request auth check: if an AdminAccount has
    been created (via /signup), a valid signed session cookie is
    required, redirecting to /login otherwise. If no account exists
    yet, falls back unchanged to the legacy DASHBOARD_PASSWORD-env-var-
    or-open behavior (_legacy_auth) -- so an existing deployment that
    hasn't signed up is unaffected, and a fresh install still works
    with zero setup the same way it always has."""
    account = db.query(AdminAccount).first()
    if not account:
        return _legacy_auth(request)

    session_token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    account_id = auth_service.verify_session_token(session_token) if session_token else None
    if account_id != account.id:
        raise NotAuthenticated(next_path=str(request.url.path))
    return account.username


def _legacy_auth(request: Request):
    """The original DASHBOARD_PASSWORD-or-open check, called directly
    (not as a FastAPI dependency) once require_auth has already
    determined no AdminAccount exists yet."""
    if os.getenv("TRUST_LOOPBACK_AS_LOCAL", "").lower() == "true":
        client_host = request.client.host if request.client else ""
        if client_host in ["127.0.0.1", "localhost", "::1"]:
            return "localhost"

    dashboard_password = os.getenv("DASHBOARD_PASSWORD")
    if not dashboard_password:
        return "none"

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Basic "):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    import base64

    try:
        decoded = base64.b64decode(auth_header[6:]).decode()
        supplied_user, _, supplied_password = decoded.partition(":")
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})

    correct_username = secrets.compare_digest(supplied_user, "admin")
    correct_password = secrets.compare_digest(supplied_password, dashboard_password)
    if not (correct_username and correct_password):
        raise HTTPException(status_code=401, detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return supplied_user


app_dependencies = [Depends(require_auth)]

app = FastAPI(title="Career Pilot -- Job Search Command Center")
app.add_middleware(CSRFMiddleware)


@app.exception_handler(NotAuthenticated)
def _handle_not_authenticated(request: Request, exc: NotAuthenticated):
    from urllib.parse import quote as _quote

    return RedirectResponse(url=f"/login?next={_quote(exc.next_path)}", status_code=303)


app.include_router(auth_router.router)

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


def _seed_demo_profile_if_needed() -> None:
    """A fresh showcase deployment shouldn't be an empty shell
    -- seed a fictional demo profile so a visitor immediately has
    something to score/tailor/explore against. Only runs in showcase
    mode, and only if no profile variant exists yet at all (never
    touches a deployment -- including this project's real personal
    instance -- that already has real profile data)."""
    if not is_showcase_mode():
        return
    db = SessionLocal()
    try:
        if db.query(ProfileVariant).first():
            return
        fixture_path = os.path.join(os.path.dirname(__file__), "fixtures", "demo_profile.json")
        with open(fixture_path, "r", encoding="utf-8") as f:
            demo_content = f.read()
        variant = profile_service.create_variant(db, "Demo Profile", is_default=True)
        profile_service.create_manual_version(db, variant.id, demo_content)
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    _seed_demo_profile_if_needed()
    bg_scheduler.start_scheduler()


@app.on_event("shutdown")
def on_shutdown():
    bg_scheduler.stop_scheduler()


@app.get("/api/health")
def health_check(db: Session = Depends(get_db)):
    """Deliberately NOT behind app_dependencies -- external
    uptime monitoring (Oracle's own health probes, UptimeRobot,
    Healthchecks.io) can't supply dashboard credentials, and a health
    endpoint gated behind the same auth as the real dashboard defeats
    its own purpose the moment DASHBOARD_PASSWORD gets set for a real
    deployment (unset today, but this app's whole point is to run
    unattended in production eventually). Checks the two things that
    actually determine whether this instance is doing its job -- real
    DB connectivity (Supabase reachability is this app's most likely
    real failure mode) and whether the background scheduler thread is
    actually alive -- rather than just whether the HTTP server accepts
    a connection, which a process can still do while its scheduler has
    silently died."""
    checks = {}
    try:
        db.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as e:
        # Fixed, generic status externally (this endpoint is deliberately
        # public, per the docstring above) -- the real exception text
        # still goes to the activity log where only an authenticated
        # dashboard view can see it, rather than handing an unauthenticated
        # caller unbounded driver/exception detail.
        checks["database"] = "unreachable"
        try:
            log_activity(db, f"Health check: database unreachable -- {e}", "ERROR")
        except Exception:
            pass

    checks["scheduler"] = "ok" if bg_scheduler.scheduler.running else "not running"

    healthy = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if healthy else 503,
        content={
            "status": "ok" if healthy else "degraded",
            "checks": checks,
            "time": utcnow().isoformat(),
        },
    )


@app.get("/", response_class=HTMLResponse, dependencies=app_dependencies)
def dashboard(request: Request, db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    total_applications = db.query(JobApplication).count()
    profile_variants = db.query(ProfileVariant).all()
    has_active_keywords = db.query(SearchKeyword).filter(SearchKeyword.is_active == True).first() is not None  # noqa: E712
    default_profile_content = profile_service.get_default_profile_content(db)
    has_profile_content = bool(default_profile_content)
    # Surfaced on the dashboard itself, not just the Profile page --
    # a real profile once went 5 days and 8 saves with a missing degree
    # and zero certifications with nothing anywhere flagging it. This is
    # the first thing anyone sees on login, so it's the right place for
    # a completeness check to actually get noticed.
    profile_completeness_warnings = (
        profile_service.profile_completeness_warnings(default_profile_content) if default_profile_content else []
    )

    return render(
        request,
        "dashboard.html",
        {
            "settings": settings,
            "total_applications": total_applications,
            "profile_variants": profile_variants,
            "backup_configured": backup_service.is_configured(),
            "showcase_mode": is_showcase_mode(),
            "intake_unconfigured": settings.automation_enabled and not has_active_keywords,
            # Onboarding call-to-action: a real profile exists, search
            # keywords are configured (ensure_intake_targeting derives
            # these automatically once a profile exists, so this is
            # normally already true by the time a profile is seeded),
            # but automation is still off -- the "reviewed everything,
            # ready to actually start" moment. Stops showing the instant
            # automation is turned on; the small nav toggle covers
            # ongoing pause/resume from there.
            "ready_to_start": has_profile_content and has_active_keywords and not settings.automation_enabled,
            "profile_completeness_warnings": profile_completeness_warnings,
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


def _check_admin_password(admin_password: str):
    configured = get_admin_password()
    if not secrets.compare_digest(admin_password, configured):
        raise HTTPException(status_code=403, detail="Incorrect admin password.")


@app.post("/settings/backup/restore/preview", dependencies=app_dependencies)
async def restore_backup_preview(
    request: Request,
    admin_password: str = Form(...),
    backup_file: UploadFile = File(...),
):
    """Step 1 of restore: upload + decrypt just far enough to
    show what the backup actually contains (row counts, when it was
    taken) -- nothing about the live database is touched here. Staged
    to a local file so the confirm step (step 2, a separate request)
    doesn't need the browser to re-upload a possibly-large file."""
    try:
        _check_admin_password(admin_password)
    except HTTPException as e:
        return RedirectResponse(url=f"/?error={quote(e.detail)}", status_code=303)

    try:
        encrypted_bytes = await backup_file.read()
        token = backup_service.stage_uploaded_backup(encrypted_bytes)
        preview = backup_service.preview_staged_backup(token)
    except RuntimeError as e:
        return RedirectResponse(url=f"/?error={quote(str(e))}", status_code=303)

    return render(request, "restore_confirm.html", {"preview": preview, "token": token})


@app.post("/settings/backup/restore/cancel", dependencies=app_dependencies)
def restore_backup_cancel(token: str = Form(...)):
    backup_service.discard_staged_backup(token)
    return RedirectResponse(url="/?message=" + quote("Restore cancelled -- nothing was changed."), status_code=303)


@app.post("/settings/backup/restore/confirm", dependencies=app_dependencies)
def restore_backup_confirm(
    admin_password: str = Form(...),
    token: str = Form(...),
    confirm_text: str = Form(...),
    db: Session = Depends(get_db),
):
    """Step 2, the actual destructive action -- requires the admin
    password again (not just carried over from step 1) and a typed
    confirmation, matching this app's existing pattern for irreversible
    actions elsewhere. execute_restore() takes a safety-net backup of
    the CURRENT database before touching anything, so a bad restore is
    itself undoable from that file."""
    try:
        _check_admin_password(admin_password)
    except HTTPException as e:
        return RedirectResponse(url=f"/?error={quote(e.detail)}", status_code=303)

    if confirm_text.strip().upper() != "RESTORE":
        return RedirectResponse(
            url=f"/?error={quote('Restore cancelled -- confirmation text did not match.')}", status_code=303
        )

    try:
        result = backup_service.execute_restore(token)
    except RuntimeError as e:
        log_activity(db, f"Database restore failed: {e}", "ERROR")
        return RedirectResponse(url=f"/?error={quote(str(e))}", status_code=303)

    log_activity(
        db,
        f"Database restored from an uploaded backup: {result['total_rows']} rows across "
        f"{len(result['row_counts'])} tables. Pre-restore safety backup saved to {result['safety_backup_path']}.",
        "WARNING",
    )
    message = (
        f"Restore complete -- {result['total_rows']} rows restored. "
        "A safety copy of your previous data was saved automatically in case you need to undo this."
    )
    return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)


@app.post("/settings/automation/toggle", dependencies=app_dependencies)
def toggle_automation(db: Session = Depends(get_db)):
    """Global kill switch -- halts crawling, tailoring, auto-apply, and
    outreach the moment it's flipped off. Every background job checks
    this fresh from the DB before doing real work. Same route backs
    both the small nav toggle (ongoing pause/resume) and the dashboard's
    "Start Hunt" onboarding button (the first-ever enable) -- it's the
    same real action either way, just reached from two different UI
    moments."""
    settings = get_or_create_settings(db)
    settings.automation_enabled = not settings.automation_enabled
    db.commit()
    message = (
        "Your job hunt has started -- intake will begin picking up new postings on its normal schedule."
        if settings.automation_enabled
        else "Automation paused."
    )
    return RedirectResponse(url=f"/?message={quote(message)}", status_code=303)


@app.post("/settings/update", dependencies=app_dependencies)
def update_settings(
    fast_poll_interval_minutes: int = Form(...),
    full_ingest_interval_minutes: int = Form(...),
    stale_posting_threshold_days: int = Form(...),
    location_query: str = Form(...),
    jobright_poll_interval_hours: int = Form(...),
    confirmation_window_hours: float = Form(...),
    fast_track_score_threshold: int = Form(...),
    fast_track_freshness_minutes: int = Form(...),
    fast_track_window_hours: float = Form(...),
    rejected_retention_days: int = Form(...),
    min_score_for_auto_launch: int = Form(...),
    tavily_monthly_call_budget: int = Form(...),
    hunter_monthly_call_budget: int = Form(...),
    daily_outreach_cap: int = Form(...),
    quiet_hours_enabled: bool = Form(False),
    quiet_hours_start_hour: int = Form(...),
    quiet_hours_end_hour: int = Form(...),
    local_timezone: str = Form(...),
    notification_digest_interval_minutes: int = Form(...),
    automated_backups_enabled: bool = Form(False),
    backup_retention_count: int = Form(14),
    db: Session = Depends(get_db),
):
    """Every tunable number in the product is editable here -- nothing
    from our design discussion is hardcoded into the app itself."""
    settings = get_or_create_settings(db)
    settings.fast_poll_interval_minutes = fast_poll_interval_minutes
    settings.full_ingest_interval_minutes = full_ingest_interval_minutes
    settings.stale_posting_threshold_days = stale_posting_threshold_days
    settings.location_query = location_query.strip() or "United States"
    settings.jobright_poll_interval_hours = jobright_poll_interval_hours
    settings.confirmation_window_hours = confirmation_window_hours
    settings.fast_track_score_threshold = fast_track_score_threshold
    settings.fast_track_freshness_minutes = fast_track_freshness_minutes
    settings.fast_track_window_hours = fast_track_window_hours
    settings.rejected_retention_days = rejected_retention_days
    settings.min_score_for_auto_launch = min_score_for_auto_launch
    settings.tavily_monthly_call_budget = tavily_monthly_call_budget
    settings.hunter_monthly_call_budget = hunter_monthly_call_budget
    settings.notification_digest_interval_minutes = notification_digest_interval_minutes
    settings.daily_outreach_cap = daily_outreach_cap
    settings.quiet_hours_enabled = quiet_hours_enabled
    settings.quiet_hours_start_hour = quiet_hours_start_hour
    settings.quiet_hours_end_hour = quiet_hours_end_hour
    settings.local_timezone = local_timezone.strip()
    settings.automated_backups_enabled = automated_backups_enabled
    settings.backup_retention_count = backup_retention_count
    db.commit()
    return RedirectResponse(url="/", status_code=303)
