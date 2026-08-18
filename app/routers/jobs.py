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
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import (
    Company,
    JobApplication,
    JobPosting,
    JobSource,
    OutreachMessage,
    SearchKeyword,
    TailoredDocument,
    get_or_create_settings,
)
from ..services import (
    confirmation_service,
    contact_discovery_service,
    intake_service,
    matching_service,
    outreach_service,
    tailoring_service,
)
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
    except (MatchingServiceError, ConfirmationServiceError) as e:
        # tailor_application hands off to confirmation_service.evaluate_and_enqueue()
        # at the end, which can raise ConfirmationServiceError -- catch both so a
        # failure there is recorded instead of dying silently in this thread.
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
    target_companies = (
        db.query(Company)
        .filter(or_(Company.greenhouse_slug.isnot(None), Company.lever_slug.isnot(None), Company.ashby_slug.isnot(None)))
        .order_by(Company.name)
        .all()
    )
    settings = get_or_create_settings(db)

    return render(
        request,
        "jobs.html",
        {
            "applications": applications,
            "sources": sources,
            "keywords": keywords,
            "target_companies": target_companies,
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


@router.post("/companies/slug")
def set_company_board_slug(
    company_name: str = Form(...),
    ats_type: str = Form(...),
    slug: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        intake_service.set_manual_board_slug(db, company_name.strip(), ats_type, slug)
        return _redirect(message=f"Set {ats_type} slug for '{company_name.strip()}'.")
    except ValueError as e:
        return _redirect(error=str(e))


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


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, db: Session = Depends(get_db)):
    """Bulk review: the primary surface for processing volume, per
    CLAUDE.md's 2026-08-17 notification-volume revision. Pending
    Confirmation (clean, safe to bulk) and Needs Review (flagged) are
    kept in structurally separate sections/forms -- not just visually --
    so a "select all" in one section can never sweep up a flagged item
    that specifically needs individual judgment."""
    pending = (
        db.query(JobApplication)
        .join(JobPosting)
        .filter(JobApplication.status == "Pending Confirmation")
        .order_by(JobApplication.confirmation_deadline.asc())
        .all()
    )
    needs_review = (
        db.query(JobApplication)
        .join(JobPosting)
        .filter(JobApplication.status == "Needs Review")
        .order_by(JobApplication.created_at.desc())
        .all()
    )

    return render(
        request,
        "review.html",
        {
            "pending": pending,
            "needs_review": needs_review,
            "message": request.query_params.get("message"),
            "error": request.query_params.get("error"),
        },
    )


def _bulk_process(db: Session, application_ids: list[int], action) -> tuple[int, list[str]]:
    succeeded = 0
    failures = []
    for application_id in application_ids:
        try:
            action(db, application_id)
            succeeded += 1
        except ConfirmationServiceError as e:
            failures.append(f"#{application_id}: {e}")
    return succeeded, failures


@router.post("/review/approve")
def review_bulk_approve(application_ids: list[int] = Form(...), db: Session = Depends(get_db)):
    succeeded, failures = _bulk_process(db, application_ids, confirmation_service.approve_application)
    message = f"Approved {succeeded} application(s)."
    if failures:
        return RedirectResponse(
            url=f"/jobs/review?error={quote(message + ' Failed: ' + '; '.join(failures))}", status_code=303
        )
    return RedirectResponse(url=f"/jobs/review?message={quote(message)}", status_code=303)


@router.post("/review/reject")
def review_bulk_reject(application_ids: list[int] = Form(...), db: Session = Depends(get_db)):
    succeeded, failures = _bulk_process(db, application_ids, confirmation_service.reject_application)
    message = f"Rejected {succeeded} application(s)."
    if failures:
        return RedirectResponse(
            url=f"/jobs/review?error={quote(message + ' Failed: ' + '; '.join(failures))}", status_code=303
        )
    return RedirectResponse(url=f"/jobs/review?message={quote(message)}", status_code=303)


def _build_detail_context(application_id: int, request: Request, db: Session, discovered_contacts=None) -> dict:
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
    outreach_messages = (
        db.query(OutreachMessage)
        .filter(OutreachMessage.application_id == application_id)
        .order_by(OutreachMessage.created_at.desc())
        .all()
    )
    settings = get_or_create_settings(db)

    return {
        "application": application,
        "posting": application.posting,
        "match_analysis": match_analysis,
        "resume_doc": resume_doc,
        "cl_doc": cl_doc,
        "outreach_messages": outreach_messages,
        "daily_outreach_cap": settings.daily_outreach_cap,
        "outreach_sent_today": outreach_service.sent_count_last_24h(db),
        "discovery_available": contact_discovery_service.is_tavily_configured(),
        "discovered_contacts": discovered_contacts,
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }


@router.get("/{application_id}", response_class=HTMLResponse)
def application_detail(application_id: int, request: Request, db: Session = Depends(get_db)):
    context = _build_detail_context(application_id, request, db)
    return render(request, "application_detail.html", context)


@router.get("/{application_id}/outreach/discover", response_class=HTMLResponse)
def discover_outreach_contacts(application_id: int, request: Request, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if not contact_discovery_service.is_tavily_configured():
        context = _build_detail_context(application_id, request, db)
        context["error"] = "Contact discovery isn't configured -- add TAVILY_API_KEY (and optionally HUNTER_API_KEY) to .env."
        return render(request, "application_detail.html", context)

    discovered = contact_discovery_service.discover_contacts(application.posting.company_name_raw)
    context = _build_detail_context(application_id, request, db, discovered_contacts=discovered)
    if not discovered:
        context["message"] = "No candidates found -- try manual entry below."
    return render(request, "application_detail.html", context)


_FINAL_STATUSES = ("Applied", "Approved", "Rejected")


@router.post("/{application_id}/score")
def score_application_now(application_id: int, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.status in _FINAL_STATUSES:
        return _redirect_detail(application_id, error=f"Application is '{application.status}' -- can't re-score a finalized application.")
    threading.Thread(target=_score_in_background, args=(application_id,), daemon=True).start()
    return _redirect_detail(application_id, message="Scoring started -- refresh in a moment to see the result.")


@router.post("/{application_id}/tailor")
def tailor_application_now(application_id: int, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.status in _FINAL_STATUSES:
        return _redirect_detail(application_id, error=f"Application is '{application.status}' -- can't re-tailor a finalized application.")
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
