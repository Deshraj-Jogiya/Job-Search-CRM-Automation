"""
Mock interview practice routes -- pick a round + tier, run the live
conversation, end it for a debrief. See app/services/mock_interview_service.py
for the actual logic; routes here just translate HTTP <-> that service.
"""

import json
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import JobApplication, MockInterviewSession
from ..services import mock_interview_service
from ..services.mock_interview_service import TIER_DESCRIPTIONS, MockInterviewServiceError
from ..templating import render

router = APIRouter(prefix="/jobs", tags=["mock-interview"])


def _redirect(application_id: int, message: str = None, error: str = None) -> RedirectResponse:
    url = f"/jobs/{application_id}/mock-interview"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/{application_id}/mock-interview", response_class=HTMLResponse)
def mock_interview_home(application_id: int, request: Request, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        return RedirectResponse(url="/jobs", status_code=303)

    rounds = mock_interview_service.get_available_rounds(db, application_id)
    sessions = mock_interview_service.list_sessions(db, application_id)
    for s in sessions:
        s.trend = None
        if s.debrief_json:
            comparison = json.loads(s.debrief_json).get("comparison") or {}
            if comparison.get("has_previous"):
                s.trend = comparison.get("trend")
    return render(
        request,
        "mock_interview_home.html",
        {
            "application": application,
            "posting": application.posting,
            "rounds": rounds,
            "sessions": sessions,
            "tiers": TIER_DESCRIPTIONS,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{application_id}/mock-interview/start")
def start_mock_interview(
    application_id: int,
    round_name: str = Form(...),
    tier: str = Form(...),
    camera_enabled: bool = Form(False),
    db: Session = Depends(get_db),
):
    try:
        session = mock_interview_service.start_session(db, application_id, round_name, tier, camera_enabled)
    except MockInterviewServiceError as e:
        return _redirect(application_id, error=str(e))
    return RedirectResponse(url=f"/jobs/{application_id}/mock-interview/{session.id}", status_code=303)


@router.get("/{application_id}/mock-interview/{session_id}", response_class=HTMLResponse)
def mock_interview_session_detail(application_id: int, session_id: int, request: Request, db: Session = Depends(get_db)):
    session = db.query(MockInterviewSession).filter(MockInterviewSession.id == session_id).first()
    if not session:
        return RedirectResponse(url=f"/jobs/{application_id}/mock-interview", status_code=303)

    debrief = json.loads(session.debrief_json) if session.debrief_json else None
    visual_metrics = json.loads(session.visual_metrics_json) if session.visual_metrics_json else {}
    tier_label, tier_description = TIER_DESCRIPTIONS.get(session.tier, (session.tier, ""))
    return render(
        request,
        "mock_interview_session.html",
        {
            "application": session.application,
            "posting": session.application.posting,
            "session": session,
            "turns": session.turns,
            "debrief": debrief,
            "visual_metrics": visual_metrics,
            "tier_label": tier_label,
            "tier_description": tier_description,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{application_id}/mock-interview/{session_id}/respond")
def respond_mock_interview(application_id: int, session_id: int, answer: str = Form(...), db: Session = Depends(get_db)):
    try:
        mock_interview_service.submit_answer(db, session_id, answer)
    except MockInterviewServiceError as e:
        return RedirectResponse(
            url=f"/jobs/{application_id}/mock-interview/{session_id}?error={quote(str(e))}", status_code=303
        )
    return RedirectResponse(url=f"/jobs/{application_id}/mock-interview/{session_id}", status_code=303)


@router.post("/{application_id}/mock-interview/{session_id}/end")
def end_mock_interview(
    application_id: int,
    session_id: int,
    frames_analyzed: int = Form(0),
    frames_face_forward: int = Form(0),
    movement_events: int = Form(0),
    db: Session = Depends(get_db),
):
    # Only ever three small numbers -- never a video frame -- submitted
    # by the client-side face tracker (see mock_interview_session.html).
    # Absent/zero when camera feedback wasn't used for this session.
    visual_metrics = (
        {"frames_analyzed": frames_analyzed, "frames_face_forward": frames_face_forward, "movement_events": movement_events}
        if frames_analyzed else None
    )
    try:
        mock_interview_service.end_session(db, session_id, visual_metrics=visual_metrics)
    except MockInterviewServiceError as e:
        return RedirectResponse(
            url=f"/jobs/{application_id}/mock-interview/{session_id}?error={quote(str(e))}", status_code=303
        )
    return RedirectResponse(url=f"/jobs/{application_id}/mock-interview/{session_id}", status_code=303)
