"""
Job search visibility and configuration -- the postings list (with
scam/staleness/repost flags surfaced as warnings, never filtered), a
manual "search now" trigger, per-source status, and search keyword
management.

Also the application detail view, plus manual "score" and "tailor"
triggers. Both are on-demand, not automatic on intake -- each is a
real LLM call with real cost (see matching_service/tailoring_service
docstrings).

Keywords/seniority-exclusions/location-exclusions share one removal
model: there is no hard-delete reachable from the UI. The only way to
remove one from active use is pause (toggle), which keeps the row --
a paused term is still visible, still selectable from the "paused"
dropdown to re-add with one click, and its exact text is never lost to
a typo'd memory. This replaced an earlier design with a real delete
button and no confirmation, which made an accidental permanent loss
one misclick away.
"""

import io
import json
import threading
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session, joinedload

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
    adaptation_service,
    autofill_service,
    behavioral_story_service,
    confirmation_service,
    contact_discovery_service,
    docx_generator,
    document_render_service,
    intake_service,
    interview_prep_service,
    matching_service,
    outreach_service,
    page_fit_service,
    tailoring_service,
    wage_level_service,
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


_STAGE_GROUPS = (
    ("needs_attention", ("Pending Confirmation", "Needs Review")),
    ("in_progress", ("Draft", "Ingested", "Tailored", "Approved")),
    ("applied", ("Applied", "Interviewing", "Offer")),
    ("closed", ("Rejected", "Not Selected")),
)
# Real gap found 2026-09-15: the Jobs page rendered every application in
# one flat reverse-chronological list, no grouping, no way to see "just
# what needs a decision right now" without scanning past everything else
# -- confirmed via real UX research that unbounded/ungrouped lists are
# right for "endless discovery" feeds, wrong for a task-driven tool like
# this one. Grouped by real pipeline stage instead; any status not
# explicitly named above (a future status added later and not yet
# sorted into a group) falls into "in_progress" rather than silently
# vanishing from the page.
_STAGE_STATUS_TO_GROUP = {status: group for group, statuses in _STAGE_GROUPS for status in statuses}


def _group_applications_by_stage(applications: list) -> dict:
    grouped = {group: [] for group, _ in _STAGE_GROUPS}
    for application in applications:
        grouped[_STAGE_STATUS_TO_GROUP.get(application.status, "in_progress")].append(application)
    return grouped


# Real gap found 2026-09-15: 354 real target companies on the live
# instance, rendered inline with zero limit, above the applications a
# user actually came to this page to check -- the single biggest
# contributor to the page feeling like an endless, purposeless scroll.
# This is a rarely-touched reference/config list (not a daily task list
# the way applications are), so a full paginated UI is more machinery
# than the real usage pattern warrants -- capped + collapsed instead.
_TARGET_COMPANIES_PREVIEW_LIMIT = 50


_KANBAN_AUTO_STATUSES = ("Ingested", "Tailored")


def _load_active_applications(db: Session) -> list:
    """Real bug found live 2026-09-22, during a full UI audit: a single
    `order_by(created_at.desc()).limit(100)` across EVERY status meant
    real, still-open Approved/Needs Review/Pending Confirmation
    applications silently fell out of the window entirely once 100
    newer Ingested rows had been created since -- confirmed live, the
    Kanban board showed "Approved 0" while 104 real Approved
    applications existed. Ingested/Tailored are the automation's own,
    high-churn, non-interactive columns (see KANBAN_COLUMNS) -- fine to
    cap by recency, since a human never acts on them directly. Every
    other status is what a human actually manages here; those are
    fetched without that same recency cap so a week-old Approved
    application stays visible instead of being crowded out by intake
    volume. This is the one place both the Kanban board and the Jobs
    list view load applications from -- fixed once, not per page."""
    base_query = db.query(JobApplication).join(JobPosting).options(joinedload(JobApplication.interview_preps))
    managed = base_query.filter(JobApplication.status.notin_(_KANBAN_AUTO_STATUSES)).order_by(
        JobApplication.created_at.desc()
    ).all()
    auto = base_query.filter(JobApplication.status.in_(_KANBAN_AUTO_STATUSES)).order_by(
        JobApplication.created_at.desc()
    ).limit(100).all()
    return managed + auto


# Real, pipeline-order column set for the Kanban board (routers/jobs.py's
# kanban_page/kanban_move) -- deliberately NOT the same 4 coarse groups as
# _STAGE_GROUPS above. Those 4 groups are right for the list view (a quick
# "what needs me right now" scan), but a Kanban column IS a status, and
# collapsing e.g. Applied/Interviewing/Offer into one column would make a
# drag ambiguous (drop into "Applied" meaning... which of the three?).
# "interactive": False marks a column the pipeline sets on its own
# (tailoring_service.py, not any human action) -- there is no
# confirmation_service function that moves an application INTO Ingested
# or Tailored by hand, so the board must not offer them as drop targets;
# see confirmation_service.KANBAN_TRANSITIONS, which this must stay a
# superset of (every interactive status here has an entry there).
KANBAN_COLUMNS = (
    ("Ingested", False),
    ("Tailored", False),
    ("Needs Review", True),
    ("Pending Confirmation", True),
    ("Approved", True),
    ("Applied", True),
    ("Interviewing", True),
    ("Offer", True),
    ("Not Selected", True),
    ("Rejected", True),
)


@router.get("", response_class=HTMLResponse)
def jobs_page(request: Request, db: Session = Depends(get_db)):
    """List and Board (Kanban) are two tabs on this one page as of
    2026-09-22 -- explicit user direction ("kanban and other related
    one to it"). Both genuinely query the same _load_active_applications
    data, just rendered two different ways (grouped-by-stage list vs.
    drag-and-drop columns), so one fetch here serves both tab panels
    (see jobs.html)."""
    applications = _load_active_applications(db)
    sources = db.query(JobSource).order_by(JobSource.name).all()
    keywords = db.query(SearchKeyword).order_by(SearchKeyword.keyword).all()
    seniority_exclusions = db.query(SeniorityExclusion).order_by(SeniorityExclusion.term).all()
    location_exclusions = db.query(LocationExclusion).order_by(LocationExclusion.term).all()
    target_companies = (
        db.query(Company)
        .filter(or_(
            Company.greenhouse_slug.isnot(None),
            Company.lever_slug.isnot(None),
            Company.ashby_slug.isnot(None),
            Company.recruitee_slug.isnot(None),
            Company.personio_slug.isnot(None),
            Company.workable_slug.isnot(None),
            Company.smartrecruiters_slug.isnot(None),
        ))
        .order_by(Company.name)
        .all()
    )
    settings = get_or_create_settings(db)

    return render(
        request,
        "jobs.html",
        {
            "application_groups": _group_applications_by_stage(applications),
            "applications_total": len(applications),
            "sources": sources,
            "keywords": keywords,
            "seniority_exclusions": seniority_exclusions,
            "location_exclusions": location_exclusions,
            "target_companies": target_companies[:_TARGET_COMPANIES_PREVIEW_LIMIT],
            "target_companies_total": len(target_companies),
            "automation_enabled": settings.automation_enabled,
            "columns": _group_applications_by_status(applications),
            "column_order": KANBAN_COLUMNS,
            "valid_source_statuses_json": json.dumps(confirmation_service.KANBAN_VALID_SOURCE_STATUSES),
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


@router.post("/postings/{posting_id}/not-a-duplicate")
def mark_posting_not_a_duplicate(posting_id: int, db: Session = Depends(get_db)):
    """b1.4 correction feedback: a posting flagged 'repost x{{n}}' was
    actually a distinct posting the fuzzy dedup wrongly merged. Nudges
    adaptation_service's self-tuned dedupe threshold higher (see
    record_dedupe_correction's docstring) and logs the correction."""
    posting = db.query(JobPosting).filter(JobPosting.id == posting_id).first()
    if not posting:
        return _redirect(error=f"Posting {posting_id} not found.")
    if posting.repost_count <= 0:
        return _redirect(error="This posting isn't flagged as a repost.")
    new_threshold = adaptation_service.record_dedupe_correction(db, was_false_merge=True)
    return _redirect(message=f"Noted -- dedupe threshold adjusted to {new_threshold:.2f}.")


@router.post("/postings/{posting_id}/sponsorship-flag-wrong")
def mark_sponsorship_flag_wrong(posting_id: int, label: str = Form(...), db: Session = Depends(get_db)):
    """b1.5 correction feedback: one blocked-sponsorship regex label
    misfired on a real posting. Accumulates toward
    adaptation_service.sponsorship_misfire_report's proposed-narrowing
    surface -- never edits or disables the pattern itself."""
    posting = db.query(JobPosting).filter(JobPosting.id == posting_id).first()
    if not posting:
        return _redirect(error=f"Posting {posting_id} not found.")
    if not posting.sponsorship_blocked:
        return _redirect(error="This posting isn't sponsorship-blocked.")
    adaptation_service.record_sponsorship_misfire(db, posting.id, label)
    return _redirect(message="Noted -- thanks, this helps tune the sponsorship pattern list.")


@router.post("/postings/{posting_id}/salary")
def set_posting_salary(posting_id: int, salary_min: int = Form(...), salary_max: int = Form(...), db: Session = Depends(get_db)):
    """Manual override for this posting's offered salary -- see
    wage_level_service.py. Always wins over salary_parser.py's own
    regex extraction from the JD text, and is never silently
    overwritten by a later re-parse."""
    posting = db.query(JobPosting).filter(JobPosting.id == posting_id).first()
    if not posting:
        return _redirect(error=f"Posting {posting_id} not found.")
    if salary_min <= 0 or salary_max <= 0 or salary_min > salary_max:
        return _redirect(error="Enter a valid salary range (min > 0, min <= max).")
    settings = get_or_create_settings(db)
    wage_level_service.set_manual_salary(db, posting, settings, salary_min, salary_max)
    return _redirect(message=f"Set offered salary for '{posting.job_title}' to ${salary_min:,}-${salary_max:,}.")


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


@router.post("/keywords/reactivate")
def reactivate_keyword(keyword_id: int = Form(...), db: Session = Depends(get_db)):
    """Backs the "paused keywords" dropdown -- picking one from the list
    and re-adding it this way means never having to retype (or remember)
    a term that was paused earlier. There is deliberately no hard-delete
    for keywords/exclusions anymore (see the Jobs page docstring note) --
    pausing via toggle is the only removal path, so nothing a user
    accidentally clicks away is ever actually unrecoverable."""
    kw = db.query(SearchKeyword).filter(SearchKeyword.id == keyword_id).first()
    if not kw:
        return _redirect(error=f"Keyword {keyword_id} not found.")
    kw.is_active = True
    db.commit()
    return _redirect(message=f"Re-added keyword '{kw.keyword}'.")


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


@router.post("/seniority-exclusions/reactivate")
def reactivate_seniority_exclusion(exclusion_id: int = Form(...), db: Session = Depends(get_db)):
    ex = db.query(SeniorityExclusion).filter(SeniorityExclusion.id == exclusion_id).first()
    if not ex:
        return _redirect(error=f"Seniority exclusion {exclusion_id} not found.")
    ex.is_active = True
    db.commit()
    return _redirect(message=f"Re-added seniority exclusion '{ex.term}'.")


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


@router.post("/location-exclusions/reactivate")
def reactivate_location_exclusion(exclusion_id: int = Form(...), db: Session = Depends(get_db)):
    ex = db.query(LocationExclusion).filter(LocationExclusion.id == exclusion_id).first()
    if not ex:
        return _redirect(error=f"Location exclusion {exclusion_id} not found.")
    ex.is_active = True
    db.commit()
    return _redirect(message=f"Re-added location exclusion '{ex.term}'.")


@router.get("/review", response_class=HTMLResponse)
def review_page(request: Request, db: Session = Depends(get_db)):
    """Bulk review: the primary surface for processing volume. Pending
    Confirmation (clean, safe to bulk) and Needs Review (flagged) are
    kept in structurally separate sections/forms -- not just visually --
    so a "select all" in one section can never sweep up a flagged item
    that specifically needs individual judgment.

    Also renders the Ready to Apply data as a second tab on the same
    page (client-side toggle, see review.html) -- explicit user
    direction 2026-09-22: these two are a real linked pair (this page
    clears things INTO Approved; Ready to Apply is exactly where
    Approved things go next), unlike Daily Triage or the List/Kanban
    pair, which stay separate. /jobs/ready-to-apply still works as a
    real redirect here (see ready_to_apply_page below), so no existing
    link/bookmark breaks."""
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
    ready_to_apply_rows = (
        db.query(JobApplication, JobPosting)
        .join(JobPosting, JobApplication.posting_id == JobPosting.id)
        .filter(JobApplication.status == "Approved")
        .order_by(JobPosting.first_seen_at.desc())
        .all()
    )

    return render(
        request,
        "review.html",
        {
            "pending": pending,
            "needs_review": needs_review,
            "ready_to_apply_rows": ready_to_apply_rows,
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

    active_prep = application.active_interview_prep
    general_prep = json.loads(active_prep.general_prep_json) if (
        active_prep and active_prep.general_prep_json
    ) else None
    company_prep = json.loads(active_prep.company_prep_json) if (
        active_prep and active_prep.company_prep_json
    ) else None
    process_research = json.loads(active_prep.process_research_json) if (
        active_prep and active_prep.process_research_json
    ) else None
    predicted_rounds = json.loads(active_prep.predicted_rounds_json) if (
        active_prep and active_prep.predicted_rounds_json
    ) else None
    prep_versions = interview_prep_service.list_interview_prep_versions(db, application_id)

    try:
        _, variant_id = matching_service.get_profile_content_for_application(db, application)
        confirmed_stories = behavioral_story_service.list_stories(db, variant_id, confirmed_only=True)
        for s in confirmed_stories:
            s.traits = json.loads(s.traits_json) if s.traits_json else []
    except matching_service.MatchingServiceError:
        confirmed_stories = []

    return {
        "application": application,
        "posting": application.posting,
        "match_analysis": match_analysis,
        "resume_doc": resume_doc,
        "cl_doc": cl_doc,
        "general_prep": general_prep,
        "company_prep": company_prep,
        "process_research": process_research,
        "predicted_rounds": predicted_rounds,
        "confirmed_stories": confirmed_stories,
        "interview_prep": active_prep,
        "prep_versions": prep_versions,
        "outreach_messages": outreach_messages,
        "discovery_available": contact_discovery_service.is_tavily_configured(),
        "discovered_contacts": discovered_contacts,
        "autofill_supported": autofill_service.is_supported(application.posting.source),
        "autofill_supported_sources": autofill_service.supported_sources(),
        "message": request.query_params.get("message"),
        "error": request.query_params.get("error"),
    }


# Real, live-caught routing bug: FastAPI/Starlette matches routes in
# REGISTRATION ORDER, not by specificity -- a GET "/kanban" registered
# AFTER the generic "/{application_id}" below it gets shadowed, since
# "/jobs/kanban" matches "/{application_id}" first (with application_id
# ="kanban", which then 422s trying to parse it as an int). This route
# must stay declared before application_detail below for that reason;
# don't move it back down.
@router.get("/kanban")
def kanban_page(request: Request):
    """Real redirect, not a page of its own, as of 2026-09-22: the
    board is now the second tab on /jobs (see jobs_page) -- explicit
    user direction to toggle List and Board together, since they query
    the same data just rendered two different ways. Kept as a real
    route (not removed) so any existing link/bookmark to the old
    standalone page still lands somewhere correct, with the right tab
    pre-selected."""
    params = dict(request.query_params)
    params["tab"] = "board"
    return RedirectResponse(url=f"/jobs?{urlencode(params)}", status_code=303)


@router.get("/ready-to-apply")
def ready_to_apply_page(request: Request):
    """Real redirect, not a page of its own, as of 2026-09-22: Ready to
    Apply is now the second tab on /jobs/review (see review_page) --
    explicit user direction that these two are a real linked pair,
    toggled together like the light/dark theme switch, unlike Daily
    Triage or the List/Kanban pair. Kept as a real route (not just
    removed) so any existing link/bookmark to the old standalone page
    still lands somewhere correct, with the right tab pre-selected.

    Registered BEFORE the /{application_id} catch-all -- Starlette
    matches routes in registration order, not by specificity, and this
    project has hit that exact shadowing bug once already (the Kanban
    board's own route, see its own history)."""
    params = dict(request.query_params)
    params["tab"] = "ready-to-apply"
    return RedirectResponse(url=f"/jobs/review?{urlencode(params)}", status_code=303)


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

    try:
        profile_content, _ = matching_service.get_profile_content_for_application(db, application)
    except matching_service.MatchingServiceError:
        profile_content = None  # overlap-based reasons just won't fire; discovery itself still works

    discovered = contact_discovery_service.discover_contacts(
        db, application.posting.company_name_raw, job_title=application.posting.job_title, profile_content=profile_content
    )
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


@router.get("/{application_id}/tailored/{document_type}/download")
def download_tailored_document(application_id: int, document_type: str, inline: bool = False, db: Session = Depends(get_db)):
    """Renders a real PDF from the same content this app already
    generates for the actual browser-autofill upload (see
    autofill_service.py, which calls these same render_* functions to
    produce the file it attaches to a real application form) -- until
    now that rendering only ever happened invisibly mid-autofill, so a
    candidate wanting to preview or manually attach the resume/cover
    letter had nothing but a raw JSON dump on the page to work with.

    `inline=True` (the template's "Preview" link) serves the exact same
    bytes with Content-Disposition: inline instead of attachment, so a
    browser's native PDF viewer renders it in a new tab instead of
    forcing a download -- this docstring already claimed "preview" as
    a use case before this existed; every response actually always
    forced a download regardless of intent."""
    if document_type not in ("resume", "cover_letter"):
        raise HTTPException(status_code=404, detail="Unknown document type")
    application = (
        db.query(JobApplication).options(joinedload(JobApplication.posting))
        .filter(JobApplication.id == application_id).first()
    )
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == document_type)
        .first()
    )
    if not doc:
        return _redirect_detail(application_id, error="Nothing tailored yet -- generate it first.")

    if document_type == "resume":
        try:
            pdf_bytes = page_fit_service.render_resume_pdf_with_fit(db, doc.content)["pdf_bytes"]
        except page_fit_service.PageFitExhaustedError as e:
            return _redirect_detail(application_id, error=str(e))
    else:
        resume_doc = (
            db.query(TailoredDocument)
            .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == "resume")
            .first()
        )
        candidate_name = json.loads(resume_doc.content).get("name", "") if resume_doc else ""
        pdf_bytes = document_render_service.render_cover_letter_pdf(doc.content, candidate_name)

    name_part = "resume" if document_type == "resume" else "cover-letter"
    filename = f"{name_part}-{application.posting.company_name_raw}-{application.posting.job_title}.pdf".replace(" ", "-")
    disposition = "inline" if inline else "attachment"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'{disposition}; filename="{filename}"'},
    )


@router.get("/{application_id}/tailored/resume/download-docx")
def download_tailored_resume_docx(application_id: int, db: Session = Depends(get_db)):
    """C7 -- a real, ATS-safe .docx alongside the existing PDF download,
    same source content (see docx_generator.py)."""
    application = (
        db.query(JobApplication).options(joinedload(JobApplication.posting))
        .filter(JobApplication.id == application_id).first()
    )
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    doc = (
        db.query(TailoredDocument)
        .filter(TailoredDocument.application_id == application_id, TailoredDocument.document_type == "resume")
        .first()
    )
    if not doc:
        return _redirect_detail(application_id, error="Nothing tailored yet -- generate it first.")

    resume_docx = docx_generator.build_resume_docx(json.loads(doc.content))
    buffer = io.BytesIO()
    resume_docx.save(buffer)
    buffer.seek(0)

    filename = f"resume-{application.posting.company_name_raw}-{application.posting.job_title}.docx".replace(" ", "-")
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
        application_id,
        # Real gap found live 2026-09-15: this said "~20-40s" -- for a
        # real company with a detailed multi-round process (confirmed
        # live: QuantumBlack's real 7-round pipeline took 5+ minutes,
        # even WITH _generate_predicted_rounds' own parallelization),
        # that estimate is off by an order of magnitude. Per-round Q&A
        # generation runs in parallel already (see that function's own
        # docstring); there's no further speedup available without
        # cutting real coverage, so the honest fix is a wider, truthful
        # estimate, not a faster promise this can't keep.
        message="Generating interview prep -- runs several real AI passes, one per predicted round. "
                "Usually 1-3 minutes; can run longer for a company with a detailed, many-round process. "
                "Refresh this page in a bit.",
    )


@router.get("/{application_id}/interview-prep/download")
def download_interview_prep_cheat_sheet(application_id: int, round_name: str = None, db: Session = Depends(get_db)):
    application = (
        db.query(JobApplication)
        .options(joinedload(JobApplication.interview_preps), joinedload(JobApplication.posting))
        .filter(JobApplication.id == application_id)
        .first()
    )
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    active_prep = application.active_interview_prep
    if not active_prep or not active_prep.predicted_rounds_json:
        return _redirect_detail(application_id, error="Generate interview prep first.")

    general_prep = json.loads(active_prep.general_prep_json or "{}")
    company_prep = json.loads(active_prep.company_prep_json or "{}")
    predicted_rounds = json.loads(active_prep.predicted_rounds_json)

    # round_name, when passed, downloads a filtered cheat sheet for just
    # that round instead of the whole process -- matches how this
    # actually gets used, one round at a time over weeks, not the whole
    # thing dumped at once every time.
    if round_name:
        matching = [r for r in predicted_rounds.get("rounds", []) if r.get("round_name") == round_name]
        if not matching:
            return _redirect_detail(application_id, error=f"Round '{round_name}' not found in this prep.")
        predicted_rounds = {**predicted_rounds, "rounds": matching}

    pdf_bytes = document_render_service.render_interview_prep_cheat_sheet_pdf(
        application.posting.job_title, application.posting.company_name_raw,
        general_prep, company_prep, predicted_rounds,
    )
    name_part = round_name or "full"
    filename = f"interview-prep-{application.posting.company_name_raw}-{application.posting.job_title}-{name_part}.pdf".replace(" ", "-")
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/{application_id}/interview-prep/{prep_id}/restore")
def restore_interview_prep(application_id: int, prep_id: int, db: Session = Depends(get_db)):
    try:
        interview_prep_service.restore_interview_prep_version(db, prep_id)
    except interview_prep_service.InterviewPrepServiceError as e:
        return _redirect_detail(application_id, error=str(e))
    return _redirect_detail(application_id, message="Restored that interview prep version.")


@router.post("/{application_id}/interview-prep/networking-insight")
def add_networking_insight(
    application_id: int,
    round_name: str = Form(...),
    insight_text: str = Form(...),
    db: Session = Depends(get_db),
):
    try:
        interview_prep_service.add_networking_insight_to_round(db, application_id, round_name, insight_text)
    except interview_prep_service.InterviewPrepServiceError as e:
        return _redirect_detail(application_id, error=str(e))
    return _redirect_detail(application_id, message=f"Added your note to the '{round_name}' round's prep.")


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


def _approve_and_maybe_launch_autofill(db: Session, application_id: int) -> tuple:
    """Shared by the detail-page 'Approve' button and the Kanban board's
    drag-to-Approved -- a single, individual approval is a deliberate
    enough decision to also launch autofill immediately, no separate
    'Open Application' click needed. Approving a flagged (Needs Review)
    application still required the human to see the flag and choose to
    approve first; this only removes the redundant second click after
    that decision, it doesn't skip the decision itself. Returns
    (application, message) so each caller can shape its own response
    (redirect vs JSON) without duplicating the autofill-launch check."""
    application = confirmation_service.approve_application(db, application_id)
    if autofill_service.is_supported(application.posting.source):
        autofill_service.launch_autofill_in_background(application_id)
        return application, (
            "Approved -- opening a real browser window to pre-fill the application. "
            "Review everything there before clicking submit yourself."
        )
    return application, "Approved."


@router.post("/{application_id}/approve")
def approve_application_now(application_id: int, db: Session = Depends(get_db)):
    try:
        _application, message = _approve_and_maybe_launch_autofill(db, application_id)
    except ConfirmationServiceError as e:
        return _redirect_detail(application_id, error=str(e))
    return _redirect_detail(application_id, message=message)


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


@router.post("/{application_id}/mark-replied")
def mark_replied_now(application_id: int, db: Session = Depends(get_db)):
    try:
        confirmation_service.mark_replied(db, application_id)
        return _redirect_detail(application_id, message="Marked as Replied.")
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


def _group_applications_by_status(applications: list) -> dict:
    """Unlike _group_applications_by_stage above, an unrecognized status
    has nowhere safe to fall -- KANBAN_COLUMNS is meant to be the exact
    real set of JobApplication.status values (see confirmation_service.py
    /tailoring_service.py for every place status is assigned), so
    silently dropping a card here would be a real bug to catch loudly
    rather than paper over."""
    columns = {status: [] for status, _ in KANBAN_COLUMNS}
    for application in applications:
        if application.status not in columns:
            raise HTTPException(500, f"Application {application.id} has an unrecognized status '{application.status}' -- KANBAN_COLUMNS needs updating.")
        columns[application.status].append(application)
    return columns


@router.post("/{application_id}/kanban-move")
def kanban_move(application_id: int, target_status: str = Form(...), db: Session = Depends(get_db)):
    """The Kanban board's ONLY write path -- deliberately never sets
    application.status directly. Dispatches through the exact same
    guarded confirmation_service functions the detail-page buttons call,
    so a drag gets the same timestamps/preconditions/side-effects
    (autofill launch on Approve, Company.ghosted_count on Not Selected,
    activity log entries) that analytics_service/metrics_service and the
    dashboard progress cards already depend on. Returns JSON, not a
    redirect -- the board is a single page the card animates within, not
    a full-page form flow like every other action route in this file."""
    if target_status == "Approved":
        action = _approve_and_maybe_launch_autofill
    else:
        transition = confirmation_service.KANBAN_TRANSITIONS.get(target_status)
        if not transition:
            return JSONResponse(
                {"ok": False, "error": f"'{target_status}' is set automatically by the pipeline, not by hand."},
                status_code=400,
            )
        action = lambda db, aid: (transition(db, aid), f"Moved to {target_status}.")

    try:
        _application, message = action(db, application_id)
    except ConfirmationServiceError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "message": message})
