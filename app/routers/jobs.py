"""
Job search visibility and configuration -- the postings list (with
scam/staleness/repost flags surfaced as warnings, never filtered), a
manual "search now" trigger, per-source status, and search keyword
management.

Also the application detail view, plus manual "score" and "tailor"
triggers. Both are on-demand, not automatic on intake -- each is a
real LLM call with real cost (see matching_service/tailoring_service
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
    LocationExclusion,
    OutreachMessage,
    SearchKeyword,
    SeniorityExclusion,
    TailoredDocument,
    get_or_create_settings,
)
from ..services import (
    autofill_service,
    confirmation_service,
    contact_discovery_service,
    intake_service,
    interview_prep_service,
    matching_service,
    outreach_service,
    tailoring_service,
)
from ..services.activity_logger import log_activity, log_exception
from ..services.confirmation_service import ConfirmationServiceError
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
    except Exception as e:
        # This manual "search now" path previously had no exception
        # handling at all -- unlike the scheduler's own automatic
        # intake calls (scheduler._run_isolated), anything that slipped
        # past run_intake_cycle's internal per-source handling would
        # crash this thread with zero visible trace anywhere, not even
        # a log entry.
        log_activity(db, f"Manual intake run failed: {e}", "ERROR")
    finally:
        db.close()


def _record_failure(db: Session, application_id: int, error: Exception):
    # attention_reason (truncated to 250 chars) is the user-visible surface
    # for this on the application's own detail page -- log_exception adds
    # the full traceback to the retained log file alongside it, since a
    # truncated one-liner is rarely enough to actually diagnose a real
    # LLM-provider or Playwright failure after the fact.
    log_exception(f"Application {application_id} background task failed: {error}")
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if application:
        application.attention_reason = str(error)[:250]
        db.commit()


def _score_in_background(application_id: int):
    db = SessionLocal()
    try:
        matching_service.score_application(db, application_id)
    except Exception as e:
        # Broad on purpose: a real LLM-provider failure (rate
        # limit, timeout, malformed response not already wrapped as
        # MatchingServiceError) previously propagated past this narrower
        # catch and crashed the thread silently -- the application just
        # sat at "Ingested" forever with no visible reason why.
        _record_failure(db, application_id, e)
    finally:
        db.close()


def _tailor_in_background(application_id: int):
    db = SessionLocal()
    try:
        tailoring_service.tailor_application(db, application_id)
    except Exception as e:
        # Broad on purpose -- same reasoning as
        # _score_in_background, plus tailor_application hands off to
        # confirmation_service.evaluate_and_enqueue() at the end, which
        # can raise ConfirmationServiceError or trigger a real autofill
        # launch with its own failure surface.
        _record_failure(db, application_id, e)
    finally:
        db.close()


def _interview_prep_in_background(application_id: int):
    db = SessionLocal()
    try:
        interview_prep_service.generate_interview_prep(db, application_id)
    except Exception as e:
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
    seniority_exclusions = db.query(SeniorityExclusion).order_by(SeniorityExclusion.term).all()
    location_exclusions = db.query(LocationExclusion).order_by(LocationExclusion.term).all()
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
            "seniority_exclusions": seniority_exclusions,
            "location_exclusions": location_exclusions,
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


@router.post("/seniority-exclusions")
def add_seniority_exclusion(term: str = Form(...), db: Session = Depends(get_db)):
    term = term.strip()
    if not term:
        return _redirect(error="Seniority exclusion term cannot be empty.")
    exists = db.query(SeniorityExclusion).filter(SeniorityExclusion.term == term).first()
    if exists:
        return _redirect(error=f"'{term}' is already in the list.")
    db.add(SeniorityExclusion(term=term, is_active=True))
    db.commit()
    return _redirect(message=f"Added seniority exclusion '{term}'.")


@router.post("/seniority-exclusions/{exclusion_id}/toggle")
def toggle_seniority_exclusion(exclusion_id: int, db: Session = Depends(get_db)):
    ex = db.query(SeniorityExclusion).filter(SeniorityExclusion.id == exclusion_id).first()
    if not ex:
        return _redirect(error=f"Seniority exclusion {exclusion_id} not found.")
    ex.is_active = not ex.is_active
    db.commit()
    return _redirect(message=f"'{ex.term}' is now {'active' if ex.is_active else 'paused'}.")


@router.post("/seniority-exclusions/{exclusion_id}/delete")
def delete_seniority_exclusion(exclusion_id: int, db: Session = Depends(get_db)):
    ex = db.query(SeniorityExclusion).filter(SeniorityExclusion.id == exclusion_id).first()
    if ex:
        db.delete(ex)
        db.commit()
    return _redirect(message="Seniority exclusion deleted.")


@router.post("/location-exclusions")
def add_location_exclusion(term: str = Form(...), db: Session = Depends(get_db)):
    term = term.strip()
    if not term:
        return _redirect(error="Location exclusion term cannot be empty.")
    exists = db.query(LocationExclusion).filter(LocationExclusion.term == term).first()
    if exists:
        return _redirect(error=f"'{term}' is already in the list.")
    db.add(LocationExclusion(term=term, is_active=True))
    db.commit()
    return _redirect(message=f"Added location exclusion '{term}'.")


@router.post("/location-exclusions/{exclusion_id}/toggle")
def toggle_location_exclusion(exclusion_id: int, db: Session = Depends(get_db)):
    ex = db.query(LocationExclusion).filter(LocationExclusion.id == exclusion_id).first()
    if not ex:
        return _redirect(error=f"Location exclusion {exclusion_id} not found.")
    ex.is_active = not ex.is_active
    db.commit()
    return _redirect(message=f"'{ex.term}' is now {'active' if ex.is_active else 'paused'}.")


@router.post("/location-exclusions/{exclusion_id}/delete")
def delete_location_exclusion(exclusion_id: int, db: Session = Depends(get_db)):
    ex = db.query(LocationExclusion).filter(LocationExclusion.id == exclusion_id).first()
    if ex:
        db.delete(ex)
        db.commit()
    return _redirect(message="Location exclusion deleted.")


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, db: Session = Depends(get_db)):
    """Bulk review: the primary surface for processing volume. Pending
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

    general_prep = json.loads(application.interview_prep.general_prep_json) if (
        application.interview_prep and application.interview_prep.general_prep_json
    ) else None
    company_prep = json.loads(application.interview_prep.company_prep_json) if (
        application.interview_prep and application.interview_prep.company_prep_json
    ) else None

    return {
        "application": application,
        "posting": application.posting,
        "match_analysis": match_analysis,
        "resume_doc": resume_doc,
        "cl_doc": cl_doc,
        "general_prep": general_prep,
        "company_prep": company_prep,
        "interview_prep": application.interview_prep,
        "outreach_messages": outreach_messages,
        "daily_outreach_cap": settings.daily_outreach_cap,
        "outreach_sent_today": outreach_service.sent_count_last_24h(db),
        "discovery_available": contact_discovery_service.is_tavily_configured(),
        "discovered_contacts": discovered_contacts,
        "autofill_supported": autofill_service.is_supported(application.posting.source),
        "autofill_supported_sources": autofill_service.supported_sources(),
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

    discovered = contact_discovery_service.discover_contacts(db, application.posting.company_name_raw)
    context = _build_detail_context(application_id, request, db, discovered_contacts=discovered)
    if not discovered:
        context["message"] = "No candidates found -- try manual entry below."
    return render(request, "application_detail.html", context)


_FINAL_STATUSES = ("Applied", "Approved", "Rejected", "Interviewing", "Offer", "Not Selected")


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


@router.post("/{application_id}/interview-prep")
def generate_interview_prep_now(application_id: int, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.status == "Rejected":
        return _redirect_detail(application_id, error="Can't generate interview prep for a Rejected application.")
    threading.Thread(target=_interview_prep_in_background, args=(application_id,), daemon=True).start()
    return _redirect_detail(
        application_id, message="Generating interview prep -- runs a couple of AI passes, refresh in ~20-40s."
    )


@router.post("/{application_id}/autofill")
def autofill_application_now(application_id: int, db: Session = Depends(get_db)):
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    if application.status != "Approved":
        return _redirect_detail(
            application_id,
            error=f"Application is '{application.status}' -- autofill only runs on Approved applications.",
        )
    if not autofill_service.is_supported(application.posting.source):
        return _redirect_detail(
            application_id,
            error=(
                f"Autofill isn't built yet for '{application.posting.source}' postings "
                f"(currently: {', '.join(autofill_service.supported_sources())})."
            ),
        )
    autofill_service.launch_autofill_in_background(application_id)
    return _redirect_detail(
        application_id,
        message="Opening a real browser window to pre-fill the application -- review everything there before clicking submit yourself.",
    )


@router.post("/{application_id}/approve")
def approve_application_now(application_id: int, db: Session = Depends(get_db)):
    """A single, individual approval (unlike the bulk review-queue
    action below) is a deliberate enough decision to also launch
    autofill immediately -- no separate 'Open Application' click
    needed. Approving a flagged (Needs Review) application still
    required the human to see the flag and choose to approve first;
    this only removes the redundant second click after that decision,
    it doesn't skip the decision itself."""
    try:
        application = confirmation_service.approve_application(db, application_id)
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))

    if autofill_service.is_supported(application.posting.source):
        autofill_service.launch_autofill_in_background(application_id)
        return _redirect_detail(
            application_id,
            message="Approved -- opening a real browser window to pre-fill the application. "
            "Review everything there before clicking submit yourself.",
        )
    return _redirect_detail(application_id, message="Approved.")


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


@router.post("/{application_id}/mark-interviewing")
def mark_interviewing_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.mark_interviewing(db, application_id)
        return _redirect_detail(application_id, message="Marked as Interviewing.")
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{application_id}/mark-offer")
def mark_offer_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.mark_offer(db, application_id)
        return _redirect_detail(application_id, message="Marked as Offer.")
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))


@router.post("/{application_id}/mark-not-selected")
def mark_not_selected_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.mark_not_selected(db, application_id)
        return _redirect_detail(application_id, message="Marked as Not Selected.")
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))
