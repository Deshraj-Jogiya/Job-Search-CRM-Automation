"""
Phase 2 routes: job intake visibility and configuration -- the ingested
postings list (with scam/staleness/repost flags surfaced as warnings,
never filtered), a manual "run intake now" trigger, per-source status,
and search keyword management (there was previously no UI for
SearchKeyword at all in this rebuild).

Phase 3 routes: the application detail view, plus manual "score" and
"tailor" triggers. Both are on-demand, not automatic on ingest -- each
is a real LLM call with real cost (see matching_service/tailoring_service
docstrings).
"""

import json
import threading
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import JobApplication, JobPosting, JobSource, SearchKeyword, TailoredDocument, get_or_create_settings
from ..services import confirmation_service, intake_service, matching_service, tailoring_service
from ..services.confirmation_service import ConfirmationServiceError
from ..services.matching_service import MatchingServiceError
from ..templating import render

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _redirect(message: str = None, error: str = None) -> RedirectResponse:
    url = "/jobs"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


def _redirect_detail(application_id: int, message: str = None, error: str = None) -> RedirectResponse:
    url = f"/jobs/{application_id}"
    if error:
        url += f"?error={quote(error)}"
    elif message:
        url += f"?message={quote(message)}"
    return RedirectResponse(url=url, status_code=303)


def _run_intake_in_background():
    db = SessionLocal()
    try:
        intake_service.run_intake_cycle(db, force=True)
    finally:
        db.close()


def _record_failure(db: Session, application_id: int, error: Exception):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if application:
        application.attention_reason = str(error)[:250]
        db.commit()


def _score_in_background(application_id: int):
    db = SessionLocal()
    try:
        matching_service.score_application(db, application_id)
    except MatchingServiceError as e:
        _record_failure(db, application_id, e)
    finally:
        db.close()


def _tailor_in_background(application_id: int):
    db = SessionLocal()
    try:
        tailoring_service.tailor_application(db, application_id)
    except MatchingServiceError as e:
        _record_failure(db, application_id, e)
    finally:
        db.close()


@router.get("", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db)):
    applications = (
        db.query(JobApplication)
        .join(JobPosting)
        .order_by(JobApplication.created_at.desc())
        .limit(100)
        .all()
    )
    sources = db.query(JobSource).order_by(JobSource.name).all()
    keywords = db.query(SearchKeyword).order_by(SearchKeyword.keyword).all()
    settings = get_or_create_settings(db)

    return render(
        request,
        "jobs.html",
        {
            "applications": applications,
            "sources": sources,
            "keywords": keywords,
            "automation_enabled": settings.automation_enabled,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/intake/run")
def run_intake_now(db: Session = Depends(get_db)):
    settings = get_or_create_settings(db)
    if not settings.automation_enabled:
        return _redirect(error="Automation is currently disabled -- enable it first (toggle on the dashboard).")
    threading.Thread(target=_run_intake_in_background, daemon=True).start()
    return _redirect(message="Intake cycle started in the background -- refresh in a moment to see results.")


@router.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int, db: Session = Depends(get_db)):
    source = db.query(JobSource).filter(JobSource.id == source_id).first()
    if not source:
        return _redirect(error=f"Source {source_id} not found.")
    source.is_active = not source.is_active
    db.commit()
    return _redirect(message=f"'{source.name}' is now {'active' if source.is_active else 'paused'}.")


@router.post("/keywords")
def add_keyword(keyword: str = Form(...), db: Session = Depends(get_db)):
    keyword = keyword.strip()
    if not keyword:
        return _redirect(error="Keyword cannot be empty.")
    exists = db.query(SearchKeyword).filter(SearchKeyword.keyword == keyword).first()
    if exists:
        return _redirect(error=f"'{keyword}' is already in the list.")
    db.add(SearchKeyword(keyword=keyword, is_active=True))
    db.commit()
    return _redirect(message=f"Added keyword '{keyword}'.")


@router.post("/keywords/{keyword_id}/toggle")
def toggle_keyword(keyword_id: int, db: Session = Depends(get_db)):
    kw = db.query(SearchKeyword).filter(SearchKeyword.id == keyword_id).first()
    if not kw:
        return _redirect(error=f"Keyword {keyword_id} not found.")
    kw.is_active = not kw.is_active
    db.commit()
    return _redirect(message=f"'{kw.keyword}' is now {'active' if kw.is_active else 'paused'}.")


@router.post("/keywords/{keyword_id}/delete")
def delete_keyword(keyword_id: int, db: Session = Depends(get_db)):
    kw = db.query(SearchKeyword).filter(SearchKeyword.id == keyword_id).first()
    if kw:
        db.delete(kw)
        db.commit()
    return _redirect(message="Keyword deleted.")


@router.get("/{application_id}", response_class=HTMLResponse)
def application_detail(application_id: int, request: Request, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    match_analysis = json.loads(application.match_analysis_json) if application.match_analysis_json else None

    resume_doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == "resume")
        .first()
    )
    cl_doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == "cover_letter")
        .first()
    )

    return render(
        request,
        "application_detail.html",
        {
            "application": application,
            "posting": application.posting,
            "match_analysis": match_analysis,
            "resume_doc": resume_doc,
            "cl_doc": cl_doc,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


@router.post("/{application_id}/score")
def score_application_now(application_id: int, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    threading.Thread(target=_score_in_background, args=(application_id,), daemon=True).start()
    return _redirect_detail(application_id, message="Scoring started -- refresh in a moment to see the result.")


@router.post("/{application_id}/tailor")
def tailor_application_now(application_id: int, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    threading.Thread(target=_tailor_in_background, args=(application_id,), daemon=True).start()
    return _redirect_detail(
        application_id, message="Tailoring started -- this runs several AI passes, refresh in ~30-60s."
    )


@router.post("/{application_id}/approve")
def approve_application_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.approve_application(db, application_id)
        return _redirect_detail(application_id, message="Approved.")
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{application_id}/reject")
def reject_application_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.reject_application(db, application_id)
        return _redirect_detail(application_id, message="Rejected.")
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{application_id}/mark-applied")
def mark_applied_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.mark_applied(db, application_id)
        return _redirect_detail(application_id, message="Marked as Applied.")
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))
