"""API for the companion browser extension (see extension/README.md) --
the real fix for the employer-wrapped/bot-blocked ATS case (Samsara:
5/5 real Playwright attempts, 0 fields filled, likely datacenter-IP bot
blocking). Running from the user's own real browser on their own real
residential IP is what actually solves that; see extension_service.py
for the field-matching logic this reuses unchanged from Playwright
autofill.

Auth is intentionally NOT the normal admin_session cookie + CSRF-cookie
flow every other route uses -- the extension calls this from its
background service worker (a chrome-extension:// origin, not a career-
pilot page), so it never receives the CSRF cookie via a real page load,
and forwarding a cookie automatically across that origin is blocked by
the cookie's own SameSite=Strict flag anyway (correctly -- that flag is
what stops a THIRD-PARTY WEBSITE from riding the user's session). The
extension instead reads the existing admin_session cookie value itself
via the privileged chrome.cookies API (bypasses SameSite/HttpOnly by
design -- an extension is not a website) and sends it explicitly as the
X-Career-Pilot-Session header below. That is verified with the exact
same auth_service.verify_session_token() every normal page already
uses -- same session, same expiry, nothing new to revoke or manage.

This also means CSRF's double-submit check does not apply and must not
be required here: a malicious third-party page cannot forge this
header's value (it has no way to read the httponly admin_session
cookie), and this app runs no CORS middleware, so a malicious page's
own script could not read this endpoint's response even if it guessed
right. See csrf.py's EXEMPT_PATH_PREFIXES, which this path is added to.
"""

import threading

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import SessionLocal, get_db
from ..models import JobApplication
from ..services import auth_service, extension_service
from ..services.activity_logger import log_exception

router = APIRouter(prefix="/api/extension", tags=["extension"])


def require_extension_session(request: Request) -> int:
    token = request.headers.get("X-Career-Pilot-Session")
    account_id = auth_service.verify_session_token(token) if token else None
    if account_id is None:
        raise HTTPException(
            status_code=401,
            detail="Not logged in -- log into Career Pilot in this browser, then reload the extension.",
        )
    return account_id


class MatchRequest(BaseModel):
    url: str


class FieldsRequest(BaseModel):
    application_id: int
    fields: list[dict]


class AddJobRequest(BaseModel):
    url: str
    job_title: str
    company_name: str
    job_description: str


@router.post("/match")
def match_current_page(
    body: MatchRequest, db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session)
):
    application = extension_service.find_fillable_application(db, body.url)
    if not application:
        return {"matched": False}
    return {"matched": True, **extension_service.application_match_summary(db, application)}


@router.post("/answers")
def field_answers(
    body: FieldsRequest, db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session)
):
    return extension_service.resolve_field_answers(db, body.application_id, body.fields)


@router.get("/documents/{application_id}/{document_type}")
def document_for_attachment(
    application_id: int, document_type: str,
    db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session),
):
    """Real PDF bytes for the content script to attach to a real
    <input type="file"> via the DataTransfer API -- see extension/
    content.js's attachDocument. Researched properly (not assumed
    impossible) after an earlier, incomplete claim that no extension
    could ever attach a file at all: setting a file input's .value to a
    path string is blocked everywhere, but assigning a real in-memory
    File (constructed from bytes the extension already legitimately
    fetched, exactly like this) via .files = dataTransfer.files is a
    real, sanctioned browser API, not a workaround for something
    forbidden."""
    try:
        pdf_bytes, filename = extension_service.render_document_for_attachment(db, application_id, document_type)
    except extension_service.ExtensionServiceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"X-Filename": filename},
    )


def _score_and_tailor_in_background(application_id: int):
    db = SessionLocal()
    try:
        extension_service.score_and_tailor_new_application(db, application_id)
    except Exception as e:
        # Broad on purpose, same reasoning as jobs.py's own
        # _tailor_in_background -- a real LLM-provider failure here must
        # never crash this thread silently and leave the application
        # stuck at "Ingested" forever with no visible reason why.
        log_exception(f"Add-job background scoring/tailoring failed for application {application_id}: {e}")
        application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        if application:
            application.attention_reason = str(e)[:250]
            db.commit()
    finally:
        db.close()


@router.post("/jobs")
def add_job(
    body: AddJobRequest, db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session)
):
    """The "+ Add This Job in One Click" popup action -- real fix for
    the biggest, most-repeated gap this session's testing found: JobRight
    and Simplify Copilot's own popups both let a user add an
    unrecognized posting and generate tailored documents without ever
    leaving the page (researched directly, not assumed). Returns fast
    (just the real DB rows) -- scoring/tailoring/approval run in a
    background thread, same pattern as jobs.py's own /score and /tailor
    buttons, since a real multi-pass LLM tailoring run takes 30-90s."""
    try:
        application = extension_service.add_job_from_page(
            db, body.url, body.job_title, body.company_name, body.job_description
        )
    except extension_service.ExtensionServiceError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    threading.Thread(target=_score_and_tailor_in_background, args=(application.id,), daemon=True).start()
    return {"application_id": application.id}


def _tailor_existing_in_background(application_id: int):
    db = SessionLocal()
    try:
        from ..services import tailoring_service

        tailoring_service.tailor_application(db, application_id)
    except Exception as e:
        log_exception(f"Extension-triggered tailoring failed for application {application_id}: {e}")
        application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
        if application:
            application.attention_reason = str(e)[:250]
            db.commit()
    finally:
        db.close()


@router.post("/applications/{application_id}/tailor")
def tailor_existing_application(
    application_id: int, db: Session = Depends(get_db), _account_id: int = Depends(require_extension_session)
):
    """The popup's "Generate Tailored Resume + Cover Letter" action for
    an application the extension already matched but hasn't tailored
    yet -- reuses the exact same tailoring_service.tailor_application
    the Jobs page's own "Tailor Now" button calls, just reachable from
    the extension's header-based auth instead of the cookie-session one.
    Deliberately ONE combined action, not two independent ones: the real
    pipeline generates both documents together in a single pass (shared
    JD-matching context), so two separate buttons that both silently
    ran the whole thing would be misleading about what each one does."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")
    threading.Thread(target=_tailor_existing_in_background, args=(application_id,), daemon=True).start()
    return {"ok": True}
