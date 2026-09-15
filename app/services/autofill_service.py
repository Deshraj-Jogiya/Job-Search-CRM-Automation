"""
Orchestrates real application-form autofill. Renders the tailored
resume/cover letter to real PDF files, launches a real, visible
browser, navigates to the real posting, and hands off to the
ATS-specific autofill module (Greenhouse, Lever, and Ashby -- see each
module's own docstring for its ATS-specific quirks).

The browser is deliberately left open at the end for the human to
review and submit themselves -- this module never clicks submit and
never interacts with a CAPTCHA. That split (automated fill, human-only
submit) isn't just a safety preference: Greenhouse forms carry a real
reCAPTCHA that only a present human can clear anyway.

While the browser stays open, a background poll watches for a
real post-submit confirmation signal (URL or page-text change matching a
curated, conservative pattern list -- never an LLM judgment call for
this) and auto-calls mark_applied() the moment one appears. This never
changes what a human sees or has to do -- it only removes the separate
manual "Mark as Applied" click for the common case where the signal is
recognized; the manual button still exists for everything else.
"""

import json
import os
import tempfile
import threading
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from ..models import JobApplication, TailoredDocument
from .activity_logger import log_activity
from .autofill.ashby_autofill import autofill_ashby_application
from .autofill.greenhouse_autofill import autofill_greenhouse_application
from .autofill.lever_autofill import autofill_lever_application
from . import page_fit_service
from .document_render_service import render_cover_letter_pdf
from .matching_service import get_profile_content_for_application

_AUTOFILL_FUNCTIONS = {
    "greenhouse": autofill_greenhouse_application,
    "lever": autofill_lever_application,
    "ashby": autofill_ashby_application,
}
_SUPPORTED_SOURCES = set(_AUTOFILL_FUNCTIONS)

# Real, confirmed hostnames each ATS serves its own hosted board from --
# used to find the real form regardless of whether it's on that hosted
# board directly, or embedded via iframe on an employer's own branded
# careers page (confirmed live: Samsara embeds a Greenhouse form at
# samsara.com/company/careers/..., not job-boards.greenhouse.io, with
# the real fields inside a nested `job-boards.greenhouse.io/embed/...`
# frame that only appears after the "Apply" click). Universal across
# all three ATSs this app fills, not a Greenhouse-only fix -- the same
# embedding pattern is realistic for any of them, even though only the
# Greenhouse case has been seen live so far. See _resolve_fill_scope.
_ATS_HOSTNAMES = {
    "greenhouse": ("job-boards.greenhouse.io", "boards.greenhouse.io"),
    "lever": ("jobs.lever.co",),
    "ashby": ("jobs.ashbyhq.com",),
}


def _hostname_matches(url: str, expected_hosts: tuple[str, ...]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in expected_hosts)


def _resolve_fill_scope(page, source: str):
    """Returns the real Playwright scope that actually contains the
    ATS's own form fields -- the top-level Page in the common case, or
    a nested Frame when the employer embeds the same ATS's form on
    their own domain instead of sending candidates to the ATS's own
    hosted board. Matched by the ATS's own real hostname (not one
    hardcoded embed URL), so this generalizes to Lever/Ashby too, not
    just the one embed pattern already confirmed live. A Frame supports
    the same .locator()/.evaluate()/.get_by_role() calls every autofill
    module already uses; each module's own _owning_page() helper covers
    the few real PAGE-level APIs (file chooser, keyboard) a Frame has
    no equivalent for.

    Falls back to the top-level page, unchanged from before this
    existed, whenever no matching frame is found -- this only ever
    narrows the search to a more specific real scope, never removes
    the page as a valid target."""
    expected_hosts = _ATS_HOSTNAMES.get(source, ())
    if not expected_hosts or _hostname_matches(page.url, expected_hosts):
        return page
    for frame in page.frames:
        if frame != page.main_frame and _hostname_matches(frame.url, expected_hosts):
            return frame
    return page

# Submission auto-detection. Deliberately mechanical (no LLM) and
# deliberately conservative -- a false positive here would mark a real
# application as "Applied" when it wasn't, which is worse than missing a
# real one and falling back to the existing manual "Mark as Applied"
# button. Checked against a captured pre-submit baseline (both URL and
# visible text) so a phrase that already happened to be on the original
# job-description/form page can't itself trigger a false match -- only
# NEW confirmation signal that appears after the human's own submit click
# counts.
_CONFIRMATION_URL_KEYWORDS = ("thank-you", "thankyou", "thanks", "confirmation", "submitted")
_CONFIRMATION_TEXT_PHRASES = (
    "thank you for applying",
    "thanks for applying",
    "application has been submitted",
    "application was submitted",
    "successfully submitted your application",
    "we've received your application",
    "we have received your application",
    "your application has been received",
    "your application has been submitted",
)
_SUBMISSION_POLL_INTERVAL_SECONDS = 4


def _page_snapshot(page) -> tuple[str, str]:
    """Best-effort (url, lowercased body text) -- returns ("", "") on any
    read failure (e.g. mid-navigation) rather than raising, since this is
    called repeatedly in a polling loop where a single bad read shouldn't
    kill the whole watch."""
    try:
        url = page.url
        text = page.inner_text("body").lower()
        return url, text
    except Exception:
        return "", ""


def _looks_like_submission_confirmation(current_url: str, current_text: str, baseline_url: str, baseline_text: str) -> bool:
    if not current_url and not current_text:
        return False
    if current_url != baseline_url:
        path = urlparse(current_url).path.lower()
        if any(keyword in path for keyword in _CONFIRMATION_URL_KEYWORDS):
            return True
    for phrase in _CONFIRMATION_TEXT_PHRASES:
        if phrase in current_text and phrase not in baseline_text:
            return True
    return False


class AutofillServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def is_supported(source: str) -> bool:
    return source in _SUPPORTED_SOURCES


def supported_sources() -> list[str]:
    return sorted(_SUPPORTED_SOURCES)


def _load_tailored_documents(db: Session, application_id: int) -> tuple[TailoredDocument, TailoredDocument | None]:
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
    if not resume_doc:
        raise AutofillServiceError("No tailored resume found -- tailor this application first.")
    return resume_doc, cl_doc


def run_autofill(db: Session, application_id: int) -> None:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise AutofillServiceError(f"Application {application_id} not found.")
    if application.status != "Approved":
        raise AutofillServiceError(
            f"Application is '{application.status}' -- autofill only runs on Approved applications."
        )

    posting = application.posting
    if not is_supported(posting.source):
        raise AutofillServiceError(
            f"Autofill isn't built yet for '{posting.source}' postings "
            f"(currently: {', '.join(supported_sources())})."
        )

    resume_doc, cl_doc = _load_tailored_documents(db, application_id)
    profile_content, _ = get_profile_content_for_application(db, application)
    resume_content = json.loads(resume_doc.content)

    # The uploaded filename an ATS shows/stores is whatever the local
    # file is named -- derived from the candidate's own real name (not
    # a hardcoded literal, so this works for any fork/profile) rather
    # than a generic "resume.pdf" that gives the reviewer no signal
    # which candidate it belongs to.
    candidate_name = resume_content.get("name") or profile_content.get("name") or "Candidate"
    safe_name = "_".join(candidate_name.split())
    tmp_dir = tempfile.mkdtemp(prefix="career_pilot_autofill_")
    resume_pdf_path = os.path.join(tmp_dir, f"{safe_name}_resume.pdf")
    try:
        resume_pdf_bytes = page_fit_service.render_resume_pdf_with_fit(db, resume_content)["pdf_bytes"]
    except page_fit_service.PageFitExhaustedError as e:
        raise AutofillServiceError(str(e)) from e
    with open(resume_pdf_path, "wb") as f:
        f.write(resume_pdf_bytes)

    cover_letter_pdf_path = None
    if cl_doc:
        cover_letter_pdf_path = os.path.join(tmp_dir, f"{safe_name}_cover_letter.pdf")
        with open(cover_letter_pdf_path, "wb") as f:
            f.write(render_cover_letter_pdf(cl_doc.content, resume_content.get("name", "")))

    log_activity(
        db,
        f"Opening a real browser to pre-fill the application for "
        f"'{posting.job_title}' at {posting.company_name_raw}...",
        "INFO",
    )

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()

        target_url = posting.job_url
        if posting.source == "lever":
            # Lever's stored job_url is the base posting page -- the real
            # application form lives at a /apply-suffixed URL, unlike
            # Greenhouse/Ashby where the form is reached by clicking an
            # in-page "Apply" button instead.
            target_url = target_url.rstrip("/") + "/apply"

        # job_url normally comes straight from the ATS's own API
        # (absolute_url/hostedUrl/jobUrl), not employer-supplied free
        # text, but this is still the one place a real browser -- with a
        # real resume attached and a human about to look at whatever
        # loads -- navigates to a URL sourced from scraped intake data.
        # Fail closed rather than letting Playwright interpret a
        # non-http(s) scheme.
        if urlparse(target_url).scheme not in ("http", "https"):
            browser.close()
            raise AutofillServiceError(f"Refusing to navigate to a non-http(s) URL: {target_url!r}")

        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

        if posting.source in ("greenhouse", "ashby"):
            # Both hosted boards often show the JD first with an "Apply"
            # button that reveals/navigates to the real form -- click it
            # if present, harmless no-op if the form is already visible.
            # Also the trigger for an embedded ATS iframe to appear (see
            # _resolve_fill_scope) -- confirmed live, it doesn't exist
            # until after this click.
            try:
                page.get_by_role("button", name="Apply", exact=False).first.click(timeout=3000)
                page.wait_for_timeout(2000)
            except Exception:
                pass

        # The real form lives on the top-level page in the common case,
        # but inside a nested frame when the employer embeds the ATS's
        # form on their own domain -- see _resolve_fill_scope's own
        # docstring. A silent zero-fields-filled result (no exception,
        # just nothing matched) was the real, live symptom this fixes:
        # every .locator() call in the autofill modules was searching
        # the top-level page for fields that only existed inside the
        # iframe.
        fill_scope = _resolve_fill_scope(page, posting.source)
        if fill_scope is not page:
            log_activity(
                db,
                f"Detected '{posting.job_title}' at {posting.company_name_raw}'s application form embedded "
                "in an iframe on the employer's own page rather than the ATS's own hosted board -- filling "
                "inside that frame.",
                "INFO",
            )

        autofill_fn = _AUTOFILL_FUNCTIONS[posting.source]
        result = autofill_fn(
            fill_scope,
            profile_content,
            resume_pdf_path,
            cover_letter_pdf_path,
            posting.job_description,
            posting.company_name_raw,
            posting.source,
        )
        eeo_filled = len(result.get("eeo", []))
        log_activity(
            db,
            f"Pre-filled application for '{posting.job_title}' at {posting.company_name_raw}: "
            f"{len(result['standard_fields'])} contact field(s), {len(result['files'])} file(s), "
            f"{eeo_filled} EEO field(s), "
            f"{len(result['custom_questions'])}/{result['custom_questions_detected']} question(s) answered. "
            "Browser left open for your review -- nothing has been submitted.",
            "INFO",
        )

        # Deliberately no browser.close() here -- the human needs the
        # window to stay open to review and submit it themselves. This
        # blocks the background thread until they close it, polling in
        # between for a real post-submit confirmation signal so a
        # genuine submission can be auto-marked as Applied without
        # waiting for a separate manual click -- see
        # _watch_for_submission_and_close.
        _watch_for_submission_and_close(db, application_id, page)


_UNRECOGNIZED_CLOSE_TEXT_SNIPPET_CHARS = 500


def _watch_for_submission_and_close(db: Session, application_id: int, page) -> None:
    baseline_url, baseline_text = _page_snapshot(page)
    last_url, last_text = baseline_url, baseline_text
    confirmed = False

    while True:
        try:
            if page.is_closed():
                if not confirmed:
                    _log_unrecognized_close(db, application_id, last_url, last_text)
                return
        except Exception:
            if not confirmed:
                _log_unrecognized_close(db, application_id, last_url, last_text)
            return

        if not confirmed:
            current_url, current_text = _page_snapshot(page)
            if current_url or current_text:
                last_url, last_text = current_url, current_text
            if _looks_like_submission_confirmation(current_url, current_text, baseline_url, baseline_text):
                confirmed = True
                _auto_mark_applied(db, application_id, current_url)

        try:
            page.wait_for_timeout(_SUBMISSION_POLL_INTERVAL_SECONDS * 1000)
        except Exception:
            return


def _log_unrecognized_close(db: Session, application_id: int, last_url: str, last_text: str) -> None:
    """The browser closed without this module's own curated pattern
    list recognizing a submission confirmation -- either the human
    closed it without submitting, or they did submit and the real
    confirmation page just doesn't match anything in
    _CONFIRMATION_URL_KEYWORDS/_CONFIRMATION_TEXT_PHRASES yet (this is
    real: it happened on a genuine Ashby submission, and no reliable
    public documentation of Ashby's exact confirmation page text/URL
    was found to add a pattern for ahead of time -- guessing one would
    risk a false positive elsewhere for no verified benefit). Logging
    the real last-seen page here turns the NEXT occurrence into real
    evidence to extend the pattern list with, instead of another guess."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        return
    snippet = (last_text or "")[-_UNRECOGNIZED_CLOSE_TEXT_SNIPPET_CHARS:]
    log_activity(
        db,
        f"Autofill browser closed for '{application.posting.job_title}' at {application.posting.company_name_raw} "
        f"without a recognized submission confirmation. If you did submit it, mark it Applied manually -- and if "
        f"you have a moment, this is real evidence to extend the pattern list with. Last URL: {last_url!r}. "
        f"Last page text (end): {snippet!r}",
        "INFO",
    )


def _auto_mark_applied(db: Session, application_id: int, confirmation_url: str) -> None:
    from .confirmation_service import ConfirmationServiceError, mark_applied

    try:
        application = mark_applied(db, application_id)
    except ConfirmationServiceError as e:
        # Application isn't in a state mark_applied() accepts (e.g. the
        # human already clicked "Mark as Applied" manually while this was
        # still polling) -- not an error worth surfacing, just skip.
        log_activity(db, f"Submission auto-detection fired but couldn't mark applied: {e}", "INFO")
        return

    note = f"[Auto-detected] Submission confirmation observed at {confirmation_url}"
    application.notes = f"{application.notes}\n{note}" if application.notes else note
    db.commit()
    log_activity(
        db,
        f"Auto-detected a real submission for '{application.posting.job_title}' at "
        f"{application.posting.company_name_raw} -- marked Applied automatically.",
        "INFO",
    )


def _record_autofill_failure(db: Session, application_id: int, error: Exception) -> None:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if application:
        application.attention_reason = str(error)[:250]
        db.commit()


def _run_autofill_background(application_id: int) -> None:
    from ..database import SessionLocal

    db = SessionLocal()
    try:
        run_autofill(db, application_id)
    except Exception as e:
        # Broad on purpose: run_autofill's real failure modes
        # aren't limited to AutofillServiceError -- a real Playwright
        # error, a PDF-rendering failure, or a profile-loading error would
        # previously propagate past a narrower except clause and crash
        # this thread silently, with no attention_reason and no visible
        # trace anywhere that autofill even attempted to run.
        _record_autofill_failure(db, application_id, e)
    finally:
        db.close()


def launch_autofill_in_background(application_id: int) -> None:
    """Starts `run_autofill()` on its own background thread with its own
    DB session -- shared by the manual 'Open Application' button
    (`app/routers/jobs.py`) and the auto-launch-on-clean-tailor path
    (`confirmation_service.evaluate_and_enqueue`), so there's one place
    that owns the thread/session lifecycle instead of two copies."""
    threading.Thread(target=_run_autofill_background, args=(application_id,), daemon=True).start()
