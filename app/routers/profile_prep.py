"""
Profile-level interview prep page (/interview-prep): the general prep for any
call or interview, not tied to an application. See
app/services/profile_prep_service.py for what it contains and why, and for how
it is kept in step with the profile.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import get_or_create_settings
from ..services import profile_prep_service
from ..services.profile_prep_service import ProfilePrepServiceError
from ..templating import render

router = APIRouter(prefix="/interview-prep", tags=["profile-interview-prep"])


@router.get("", response_class=HTMLResponse)
def profile_prep_page(request: Request, db: Session = Depends(get_db)):
    try:
        profile, variant = profile_prep_service.load_profile(db)
    except ProfilePrepServiceError as e:
        return render(request, "profile_prep.html", {"setup_error": str(e), "message": None, "error": None})

    # Opening the page with nothing drafted for the current profile starts the work (the same catch-up the
    # hourly scheduler runs), so the first visit after setup, or after a profile change, never sits empty
    # waiting for a click. Honours the auto-refresh switch and the failure cooldown.
    prep_status = profile_prep_service.status(db)
    if prep_status["state"] != "current" and not prep_status["generating"]:
        profile_prep_service.catch_up_in_background()
        prep_status = profile_prep_service.status(db)

    prep, drafted = profile_prep_service.get_active_prep(db, variant.id)
    return render(
        request,
        "profile_prep.html",
        {
            "setup_error": None,
            "reference": profile_prep_service.build_reference(db, profile, variant.id),
            "prep": prep,
            "drafted": drafted,
            "prep_status": prep_status,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/generate")
def generate_profile_prep_now(db: Session = Depends(get_db)):
    """The manual Refresh button: always runs (ignores the failure cooldown and the auto-refresh switch)."""
    try:
        profile_prep_service.load_profile(db)
    except ProfilePrepServiceError as e:
        return RedirectResponse(url=f"/interview-prep?error={quote(str(e))}", status_code=303)
    started = profile_prep_service.start_background_generation("manual")
    message = (
        "Refreshing from your latest profile -- one AI pass over your profile and confirmed stories. "
        "Usually under a minute or two. Refresh this page in a bit."
        if started else "A refresh is already running -- check back in a minute."
    )
    return RedirectResponse(url="/interview-prep?message=" + quote(message), status_code=303)


@router.post("/auto-refresh")
def set_auto_refresh(auto_refresh: bool = Form(False), db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    settings.profile_prep_auto_refresh = auto_refresh
    db.commit()
    return RedirectResponse(
        url="/interview-prep?message=" + quote(
            "Auto-refresh is on: drafted answers follow every profile change." if auto_refresh
            else "Auto-refresh is off: use the Refresh button when you want it updated."
        ),
        status_code=303,
    )
