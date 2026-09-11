"""
The daily triage queue (/queue) -- see queue_service.py for the real
logic. This route is read-heavy (3 tabs + counters) plus 3 mutating
actions (Skip, Not a Fit, Undo Skip); "Apply" is deliberately just a
link to the existing per-application detail page
(/jobs/{application_id}), not a new endpoint -- that page already owns
the real score/tailor/autofill/approve flow, no reason to duplicate it.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import queue_service
from ..templating import render

router = APIRouter(prefix="/queue", tags=["queue"])


@router.get("", response_class=HTMLResponse)
def queue_page(request: Request, db: Session = Depends(get_db)):
    tabs = queue_service.build_queue(db)
    counters = queue_service.daily_counters(db)
    return render(
        request,
        "queue.html",
        {
            "tabs": tabs,
            "counters": counters,
            "skip_reasons": queue_service.SKIP_REASONS,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


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
