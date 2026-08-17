"""
Phase 2 routes: job intake visibility and configuration -- the ingested
postings list (with scam/staleness/repost flags surfaced as warnings,
never filtered), a manual "run intake now" trigger, per-source status,
and search keyword management (there was previously no UI for
SearchKeyword at all in this rebuild).
"""

import threading
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import JobApplication, JobPosting, JobSource, SearchKeyword, get_or_create_settings
from ..services import intake_service
from ..templating import render

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _redirect(message: str = None, error: str = None) -> RedirectResponse:
    url = "/jobs"
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
