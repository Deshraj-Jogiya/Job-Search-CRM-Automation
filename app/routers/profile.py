"""
Living profile management routes -- variants, portfolio sync,
LinkedIn paste-diff approval flow. See app/services/profile_service.py
for the actual logic; routes here just translate HTTP <-> that service
and turn ProfileServiceError into a friendly redirect message instead of
a 500.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ProfileVariant, ProfileVersion
from ..services import profile_service
from ..services.profile_service import ProfileServiceError
from ..templating import render

router = APIRouter(prefix="/profile", tags=["profile"])


def _redirect(message: str = None, error: str = None) -> RedirectResponse:
    url = "/profile"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("", response_class=HTMLResponse)
def profile_page(request: Request, db: Session = Depends(get_db)):
    variants = db.query(ProfileVariant).order_by(ProfileVariant.created_at).all()

    variant_data = []
    for variant in variants:
        versions = (
            db.query(ProfileVersion)
            .filter(ProfileVersion.variant_id == variant.id)
            .order_by(ProfileVersion.created_at.desc())
            .all()
        )
        active_version = next((v for v in versions if v.is_active), None)
        pending_versions = [v for v in versions if not v.is_active and v.source == "linkedin_diff"]
        active_content = json.loads(active_version.content_json) if active_version else {}
        variant_data.append(
            {
                "variant": variant,
                "active_version": active_version,
                "pending_versions": pending_versions,
                "versions": versions,
                "eeo": active_content.get("eeo") or {},
                "application_preferences": active_content.get("application_preferences") or {},
            }
        )

    return render(
        request,
        "profile.html",
        {
            "variant_data": variant_data,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/variants")
def create_variant(
    name: str = Form(...),
    is_default: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        profile_service.create_variant(db, name, is_default)
        return _redirect(message=f"Created variant '{name}'.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/variants/{variant_id}/set-default")
def set_default_variant(variant_id: int, db: Session = Depends(get_db)):
    try:
        variant = profile_service.set_default_variant(db, variant_id)
        return _redirect(message=f"'{variant.name}' is now the default variant.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/variants/{variant_id}/delete")
def delete_variant(variant_id: int, db: Session = Depends(get_db)):
    try:
        profile_service.delete_variant(db, variant_id)
        return _redirect(message="Variant deleted.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/variants/{variant_id}/manual-save")
def manual_save(variant_id: int, content_json: str = Form(...), db: Session = Depends(get_db)):
    try:
        profile_service.create_manual_version(db, variant_id, content_json)
        return _redirect(message="Profile content saved.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/variants/{variant_id}/preferences")
def update_preferences(
    variant_id: int,
    gender: str = Form(""),
    hispanic_or_latino: str = Form(""),
    race: str = Form(""),
    veteran_status: str = Form(""),
    disability_status: str = Form(""),
    work_authorization: str = Form(""),
    visa_sponsorship: str = Form(""),
    willing_to_relocate: str = Form(""),
    salary_minimum: str = Form(""),
    salary_negotiable: bool = Form(False),
    notice_period: str = Form(""),
    currently_employed: str = Form(""),
    db: Session = Depends(get_db),
):
    """Structured, form-based alternative to pasting raw JSON -- these
    are all fixed personal facts (same posture as `contact`), mechanically
    answered by the autofill modules via app/services/autofill/
    common_answers.py rather than routed through the per-JD LLM draft
    call, which has no real basis to answer "what's your gender?" or
    "do you require visa sponsorship?" from a job description. Every
    field is a dropdown of real, common option text on the Profile page
    (not free text) -- salary is stored as separate minimum/negotiable
    values rather than one pre-combined string, so the form can show
    each control's own actual current state on reload without any
    string-parsing round trip."""
    eeo = {
        k: v for k, v in {
            "gender": gender.strip(),
            "hispanic_or_latino": hispanic_or_latino.strip(),
            "race": race.strip(),
            "veteran_status": veteran_status.strip(),
            "disability_status": disability_status.strip(),
        }.items() if v
    }
    application_preferences = {
        k: v for k, v in {
            "work_authorization": work_authorization.strip(),
            "visa_sponsorship": visa_sponsorship.strip(),
            "willing_to_relocate": willing_to_relocate.strip(),
            "salary_minimum": salary_minimum.strip(),
            "notice_period": notice_period.strip(),
            "currently_employed": currently_employed.strip(),
        }.items() if v
    }
    if salary_negotiable:
        application_preferences["salary_negotiable"] = True
    try:
        profile_service.update_structured_fields(
            db, variant_id, {"eeo": eeo, "application_preferences": application_preferences}
        )
        return _redirect(message="Application preferences saved.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/variants/{variant_id}/sync-portfolio")
def sync_portfolio(variant_id: int, db: Session = Depends(get_db)):
    try:
        profile_service.sync_from_portfolio(db, variant_id)
        return _redirect(message="Synced profile from portfolio.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/variants/{variant_id}/linkedin-import")
def linkedin_import(variant_id: int, pasted_text: str = Form(...), db: Session = Depends(get_db)):
    try:
        profile_service.propose_linkedin_diff(db, variant_id, pasted_text)
        return _redirect(message="LinkedIn diff proposed -- review and approve below.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/versions/{version_id}/approve")
def approve_version(version_id: int, db: Session = Depends(get_db)):
    try:
        profile_service.approve_version(db, version_id)
        return _redirect(message="Profile version approved and activated.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))


@router.post("/versions/{version_id}/reject")
def reject_version(version_id: int, db: Session = Depends(get_db)):
    try:
        profile_service.reject_version(db, version_id)
        return _redirect(message="Pending profile version rejected.")
    except ProfileServiceError as e:
        return _redirect(error=str(e))
