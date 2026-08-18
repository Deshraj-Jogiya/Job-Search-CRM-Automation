"""
Phase 5 routes: recruiter outreach, tied to a specific application.
Draft -> Approve -> Send (email) or Draft -> Approve -> Mark as Sent
(LinkedIn, manual -- see outreach_service's module docstring for why).
No timers anywhere here -- every state change is a live, explicit
click, same safety posture as the service layer underneath.
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..services import outreach_service
from ..services.outreach_service import OutreachServiceError

router = APIRouter(prefix="/jobs/{application_id}/outreach", tags=["outreach"])


def _redirect_detail(application_id: int, message: str = None, error: str = None) -> RedirectResponse:
    url = f"/jobs/{application_id}"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


@router.post("")
def draft_outreach(
    application_id: int,
    recipient_name: str = Form(""),
    recipient_address: str = Form(""),
    channel: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        outreach_service.draft_outreach_message(db, application_id, recipient_name, recipient_address, channel)
        return _redirect_detail(application_id, message="Outreach message drafted -- review before approving.")
    except OutreachServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{message_id}/approve")
def approve_outreach(application_id: int, message_id: int, db: Session = Depends(get_db)):
    try:
        outreach_service.approve_outreach(db, message_id)
        return _redirect_detail(application_id, message="Outreach message approved.")
    except OutreachServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{message_id}/reject")
def reject_outreach(application_id: int, message_id: int, db: Session = Depends(get_db)):
    try:
        outreach_service.reject_outreach(db, message_id)
        return _redirect_detail(application_id, message="Outreach message rejected.")
    except OutreachServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{message_id}/send")
def send_outreach(application_id: int, message_id: int, db: Session = Depends(get_db)):
    try:
        outreach_service.send_outreach(db, message_id)
        return _redirect_detail(application_id, message="Email sent.")
    except OutreachServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{message_id}/mark-sent")
def mark_outreach_sent(application_id: int, message_id: int, db: Session = Depends(get_db)):
    try:
        outreach_service.mark_sent_manually(db, message_id)
        return _redirect_detail(application_id, message="Marked as sent.")
    except OutreachServiceError as e:
        return _redirect_detail(application_id, error=str(e))
