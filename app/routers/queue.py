"""
The daily triage queue (/queue) -- see queue_service.py for the real
logic. This route is read-heavy (3 tabs + counters) plus 3 mutating
actions (Skip, Not a Fit, Undo Skip); "Apply" is deliberately just a
link to the existing per-application detail page
(/jobs/{application_id}), not a new endpoint -- that page already owns
the real score/tailor/autofill/approve flow, no reason to duplicate it.
"""

from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import queue_service

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("")
def queue_page(request: Request):
    """Folded into the unified /jobs hub (2026-09-22) as the Daily
    Triage tab -- redirects there instead of rendering its own page
    now, so old links/bookmarks still work, they just land
    pre-selected on the right tab."""
    params = dict(request.query_params)
    params["tab"] = "triage"
    return RedirectResponse(url=f"/jobs?{urlencode(params)}", status_code=303)


@router.post("/{application_id}/skip")
def skip(application_id: int, reason: str = Form(...), db: Session = Depends(get_db)):
    try:
        queue_service.skip_application(db, application_id, reason)
        return RedirectResponse(url="/queue?message=" + quote("Skipped."), status_code=303)
    except queue_service.QueueServiceError as e:
        return RedirectResponse(url="/queue?error=" + quote(str(e)), status_code=303)


@router.post("/{application_id}/not-a-fit")
def not_a_fit(application_id: int, reason: str = Form(...), db: Session = Depends(get_db)):
    try:
        queue_service.mark_not_a_fit(db, application_id, reason)
        return RedirectResponse(url="/queue?message=" + quote("Marked not a fit."), status_code=303)
    except queue_service.QueueServiceError as e:
        return RedirectResponse(url="/queue?error=" + quote(str(e)), status_code=303)


@router.post("/{application_id}/unskip")
def unskip(application_id: int, db: Session = Depends(get_db)):
    try:
        queue_service.unskip_application(db, application_id)
        return RedirectResponse(url="/queue?message=" + quote("Restored to queue."), status_code=303)
    except queue_service.QueueServiceError as e:
        return RedirectResponse(url="/queue?error=" + quote(str(e)), status_code=303)
