"""
Token-authenticated confirmation routes -- reachable from the one-click
email link without needing the dashboard's session/CSRF cookie. See
app/services/confirmation_tokens.py for why this is a separate auth
model, and app/csrf.py's EXEMPT_PATH_PREFIXES for how these routes
opt out of the cookie-based CSRF check.

Deliberately NOT under the /jobs prefix -- these are meant to be short,
memorable, and obviously distinct from the cookie-authenticated
dashboard routes in app/routers/jobs.py, which serve the same actions
for a user who's already logged into the dashboard in their browser.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import JobApplication
from ..services import autofill_service, confirmation_tokens
from ..services.confirmation_service import ConfirmationServiceError, approve_application, reject_application
from ..templating import templates

router = APIRouter(prefix="/confirm", tags=["confirmation"])


def _require_valid_token(application_id: int, token: str, db: Session) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if not confirmation_tokens.verify_token(token, application_id):
        raise HTTPException(status_code=403, detail="This link is invalid or has expired.")
    return application


@router.get("/{application_id}", response_class=HTMLResponse)
def confirm_page(application_id: int, token: str, request: Request, db: Session = Depends(get_db)):
    application = _require_valid_token(application_id, token, db)
    return templates.TemplateResponse(
        request,
        "confirm.html",
        {
            "application": application,
            "posting": application.posting,
            "token": token,
            "message": request.query_params.get("message"),
        },
    )


@router.post("/{application_id}/approve")
def confirm_approve(application_id: int, token: str = Form(...), db: Session = Depends(get_db)):
    application = _require_valid_token(application_id, token, db)
    try:
        approve_application(db, application_id)
    except ConfirmationServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Same reasoning as jobs.py's approve_application_now: a single,
    # deliberate approval (the human already saw the flag, if any, and
    # chose to proceed) is enough to also launch autofill immediately,
    # no separate manual step needed.
    if autofill_service.is_supported(application.posting.source):
        autofill_service.launch_autofill_in_background(application_id)
        message = "Approved -- opening a real browser window on the server to pre-fill the application."
    else:
        message = "Approved."
    return RedirectResponse(url=f"/confirm/{application_id}?token={token}&message={quote(message)}", status_code=303)


@router.post("/{application_id}/reject")
def confirm_reject(application_id: int, token: str = Form(...), db: Session = Depends(get_db)):
    _require_valid_token(application_id, token, db)
    try:
        reject_application(db, application_id)
    except ConfirmationServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url=f"/confirm/{application_id}?token={token}&message=Rejected.", status_code=303)
