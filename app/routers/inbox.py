"""
Inbox page (/inbox): job emails read from the mailbox and what each one means for an application. Every change is
the user's click; see app/services/email_sync_service.py.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..app_mode import is_showcase_mode
from ..database import get_db
from ..models import get_or_create_settings
from ..services import email_reader_service, email_sync_service, profile_service
from ..services.email_sync_service import EmailSyncError
from ..templating import render

router = APIRouter(prefix="/inbox", tags=["inbox"])

_DEMO_READ_ONLY = "The public demo's inbox is a fixed sample; nothing in it can be changed."


def _back(message: str | None = None, error: str | None = None, anchor: str = "") -> RedirectResponse:
    query = f"?message={quote(message)}" if message else f"?error={quote(error)}" if error else ""
    return RedirectResponse(url=f"/inbox{query}{anchor}", status_code=303)


@router.get("", response_class=HTMLResponse)
def inbox_page(request: Request, db: Session = Depends(get_db)):
    groups = email_sync_service.inbox(db)
    profile = profile_service.get_default_profile_content(db) or {}
    for item in groups["handled"]:
        item["feedback"] = email_sync_service.feedback_draft(item["row"], item["app"], profile.get("name"))
    return render(request, "inbox.html", {
        "groups": groups,
        "settings": get_or_create_settings(db),
        "configured": email_reader_service.is_configured(),
        "kinds": [k for k in email_sync_service.KIND_LABELS if k != "ignore"],
        "kind_labels": email_sync_service.KIND_LABELS,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    })


def _guard():
    return _back(error=_DEMO_READ_ONLY) if is_showcase_mode() else None


@router.post("/check")
def check_now(db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    try:
        counts = email_sync_service.run_sync(db, force=True)
    except EmailSyncError as e:
        return _back(error=str(e))
    return _back(message=f"Checked: {counts.get('read', 0)} emails read, {counts.get('suggested', 0)} new to review, "
                         f"{counts.get('noted', 0)} needed no change.")


@router.post("/{email_id}/accept")
def accept(email_id: int, application_id: int | None = Form(None), db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    try:
        row = email_sync_service.accept(db, email_id, application_id)
    except EmailSyncError as e:
        return _back(error=str(e))
    return _back(message=f"Done: {row.action_taken}.")


@router.post("/accept-selected")
def accept_selected(email_ids: list[int] = Form([]), db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    if not email_ids:
        return _back(error="Tick at least one email first.")
    done, errors = email_sync_service.accept_many(db, email_ids)
    if errors:
        return _back(error=f"{done} applied; {len(errors)} could not be: " + "; ".join(errors[:5]))
    return _back(message=f"{done} change{'s' if done != 1 else ''} applied.")


@router.post("/{email_id}/add")
def add(email_id: int, company: str = Form(""), role: str = Form(""), db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    try:
        app = email_sync_service.add_to_crm(db, email_id, company, role)
    except EmailSyncError as e:
        return _back(error=str(e))
    return _back(message=f"Added '{app.posting.job_title}' at {app.posting.company_name_raw} (now {app.status}).")


@router.post("/{email_id}/dismiss")
def dismiss(email_id: int, db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    try:
        email_sync_service.dismiss(db, email_id)
    except EmailSyncError as e:
        return _back(error=str(e))
    return _back(message="Dismissed. Nothing in the CRM changed.")


@router.post("/{email_id}/restore")
def restore(email_id: int, db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    try:
        email_sync_service.restore(db, email_id)
    except EmailSyncError as e:
        return _back(error=str(e))
    return _back(message="Back in the list to review.")


@router.post("/{email_id}/kind")
def set_kind(email_id: int, kind: str = Form(...), db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    try:
        email_sync_service.set_kind(db, email_id, kind)
    except EmailSyncError as e:
        return _back(error=str(e))
    return _back(message="Type changed.")


@router.post("/settings")
def save_settings(email_sync_enabled: bool = Form(False), email_sync_lookback_days: int = Form(60),
                  db: Session = Depends(get_db)):
    if (blocked := _guard()):
        return blocked
    settings = get_or_create_settings(db)
    settings.email_sync_enabled = email_sync_enabled
    settings.email_sync_lookback_days = max(1, min(email_sync_lookback_days, 365))
    db.commit()
    return _back(message="Email reading is on: it checks every "
                         f"{email_sync_service.SYNC_EVERY_MINUTES} minutes." if email_sync_enabled
                 else "Email reading is off.")
