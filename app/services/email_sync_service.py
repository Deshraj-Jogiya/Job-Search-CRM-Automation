"""
Job emails into the CRM: reads new mail (email_reader_service, read-only), sorts it by rules (email_classifier, no AI,
no paid calls), finds the application it is about (email_matcher), and keeps a row per email for the /inbox page.

Nothing changes an application until the user accepts it on /inbox, with two exceptions: a high-confidence
"application received" email matched to one application marks it Applied, and a high-confidence "not moving forward"
marks it Not Selected, on their own (auto_accept_confirmations). Interviews, offers and replies always wait.
Every change goes through
confirmation_service's guarded functions with the email's own date, so the funnel dates (applied_at, not_selected_at,
...) are when it happened, not when the button was clicked. Nothing is ever deleted: an employer's "no" becomes
Not Selected, never the user's own "Rejected".

Switched on by GlobalSettings.email_sync_enabled (off by default), separate from automation_enabled because it only
reads and suggests. Never runs in showcase mode: the demo has no mailbox, and its inbox shows sample rows built by the
same rules (demo_seed_service).
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..app_mode import is_showcase_mode
from ..database import utcnow
from ..models import EmailMessage, JobApplication, JobPosting, get_or_create_settings
from . import confirmation_service, email_classifier, email_matcher, email_reader_service
from .activity_logger import log_activity
from .confirmation_service import ConfirmationServiceError
from .email_reader_service import EmailReaderError, RawEmail
from .intake_service import get_or_create_company

EXCERPT_CHARS = 320
SYNC_EVERY_MINUTES = 30
_JOBISH_SUBJECT_RE = re.compile(
    r"applica|applying|applied|interview|candida|position|recruit|hiring|offer|assessment|\brole\b|opportunit|"
    r"thank you for|thanks for|your interest|next step|update|status|regarding your|follow-up|screen", re.I)

KIND_LABELS = {
    "confirmation": "Application received", "rejection": "Not moving forward", "interview": "Interview request",
    "assessment": "Assessment", "offer": "Offer", "reply": "Reply from a person", "ignore": "Not a job update",
}


class EmailSyncError(Exception):
    pass


@dataclass
class Suggestion:
    key: str
    label: str
    steps: list[tuple[str, Callable]]  # (field the step sets, guarded confirmation_service function)
    warning: str | None = None


# ---------------------------------------------------------------------------- reading

def _wants(owner: str, index: email_matcher.CompanyIndex, people: dict) -> Callable[[RawEmail], bool]:
    """From headers alone: is this message worth downloading? Known job senders, tracked companies' domains and
    people already contacted always are; anything else only when the subject reads like a job email."""
    def wants(raw: RawEmail) -> bool:
        if not raw.sender_email or raw.sender_email == owner:
            return False
        if raw.sender_email in people:
            return True
        if email_classifier.is_noise_sender(raw):
            return False
        if email_classifier.is_ats_sender(raw) or index.companies_for_domain(raw.sender_domain):
            return True
        return bool(_JOBISH_SUBJECT_RE.search(raw.subject or ""))
    return wants


def _excerpt(raw: RawEmail, cls: email_classifier.Classification) -> str | None:
    """A short window around the phrase that decided the kind, so the inbox can show why. None for account mail."""
    if cls.account_mail:
        return None
    text = email_classifier.flat(raw.text)
    phrase = re.search(r"'([^']+)'", cls.reason or "")
    at = text.lower().find(phrase.group(1)) if phrase else -1
    if at < 0:
        return text[:EXCERPT_CHARS] or None
    start = max(0, at - EXCERPT_CHARS // 2)
    return ("..." if start else "") + text[start:start + EXCERPT_CHARS] + ("..." if start + EXCERPT_CHARS < len(text) else "")


def ingest(db: Session, emails: list[RawEmail]) -> dict:
    """Sort, match and store emails not seen before. Separate from the mailbox read so tests and the demo can feed it."""
    index = email_matcher.CompanyIndex(db)
    people = email_matcher.known_people(db)
    seen = {m for (m,) in db.query(EmailMessage.message_id).filter(
        EmailMessage.message_id.in_([e.message_id for e in emails])).all()} if emails else set()
    counts = {"read": len(emails), "stored": 0, "suggested": 0, "noted": 0, "ignored": 0}
    stored: list[EmailMessage] = []
    for raw in sorted(emails, key=lambda e: e.received_at):
        if raw.message_id in seen:
            continue
        seen.add(raw.message_id)
        known = raw.sender_email in people
        tracked = bool(index.companies_for_domain(raw.sender_domain))
        cls = email_classifier.classify(raw, known_person=known, tracked_company_sender=tracked)
        if cls.kind == "ignore" and not (email_classifier.is_ats_sender(raw) or tracked or known):
            continue  # unrelated mail is not stored at all
        row = EmailMessage(
            message_id=raw.message_id, thread_id=raw.thread_id, received_at=raw.received_at,
            sender_name=(raw.sender_name or "")[:200] or None, sender_email=raw.sender_email,
            subject=(raw.subject or "")[:300], excerpt=_excerpt(raw, cls), kind=cls.kind, confidence=cls.confidence,
            reason=(cls.reason or "")[:300], company_guess=cls.company or (cls.hints[0] if cls.hints else None),
            role_guess=cls.role, job_ref=cls.job_ref, due_text=cls.due,
        )
        if cls.kind == "ignore":
            row.state, row.match_state = "ignored", "unmatched"
        else:
            result = email_matcher.match(index, people, raw, cls)
            row.match_state, row.match_how = result.state, result.how[:300]
            row.application_id, row.candidate_ids = result.application_id, result.candidate_ids or None
        db.add(row)
        stored.append(row)
    db.flush()
    _link_by_job_ref(db, stored)
    for row in stored:
        if row.state == "ignored":
            counts["ignored"] += 1
            continue
        _settle_if_nothing_to_do(db, row)
        counts["noted" if row.state == "noted" else "suggested"] += 1
    counts["stored"] = len(stored)
    db.commit()
    # Older waiting emails are matched again each time: an application added since, or one settled by this run (IBM's
    # other application marked Not Selected leaves one sent), can turn a "choose" into one clear match.
    auto = 0
    for _ in range(3):
        rematch_pending(db)
        step = auto_accept_confirmations(db)
        auto += step
        if not step:
            break
    counts["auto_applied"] = auto
    counts["suggested"] -= min(auto, counts["suggested"])
    return counts


AUTO_LABEL = "Marked as Applied automatically (the employer confirmed the application)"
AUTO_REJECT_LABEL = "Marked as Not Selected automatically (the employer said no)"
# Which suggestions apply on their own, per email kind. Confirmations (his call, 2026-11-20) and rejections (his call,
# 2026-11-21: "applied and rejected, nothing I can do... rejected is rejected") only record what already happened.
# Interviews, assessments, offers and replies still wait on /inbox: they ask him to do something.
_AUTO_KEYS = {"confirmation": {"applied"}, "rejection": {"not_selected", "applied_not_selected"}}


def _contacts_for(db: Session, app_id: int) -> list[str]:
    from ..models import OutreachMessage
    names = [n for (n,) in db.query(OutreachMessage.recipient_name).filter(
        OutreachMessage.application_id == app_id, OutreachMessage.status == "Sent").distinct().all() if n]
    return sorted(set(names))


def _sure_of_company(row: EmailMessage) -> bool:
    """A rejection is "medium" only because it came from the employer's own address (no-reply@perpay.com) rather than
    an ATS or a known person. It still applies on its own when the company was found by its exact name, a job ID or
    the sender: the "not moving forward" wording itself is never a weak line (those make no rejection at all)."""
    how = row.match_how or ""
    return "(exact)" in how or "job ID" in how or how.startswith("sender")


def auto_accept_confirmations(db: Session) -> int:
    """Employer emails that only say what already happened are applied without a click: "we received your
    application" marks it Applied, "we will not move forward" marks it Not Selected (Applied first, dated from the
    confirmation, when it was never marked). Only high-confidence emails matched to one application; an Offer is never
    touched; anything that could be about several applications still waits on /inbox. A rejection from the
    employer's own address counts when the company was matched exactly (`_sure_of_company`). A rejection on an application
    with people he contacted is logged with their names, the only ones worth asking for feedback. Covers older
    waiting emails too."""
    done = 0
    rows = (db.query(EmailMessage).filter(EmailMessage.state == "pending", EmailMessage.kind.in_(list(_AUTO_KEYS)),
                                          EmailMessage.confidence.in_(("high", "medium")),
                                          EmailMessage.match_state == "matched", EmailMessage.application_id.isnot(None))
            .order_by(EmailMessage.received_at).all())
    for row in rows:
        if row.confidence != "high" and not (row.kind == "rejection" and _sure_of_company(row)):
            continue
        db.refresh(row)
        if row.state != "pending":
            continue  # settled by an earlier one for the same application
        app = db.get(JobApplication, row.application_id)
        if app is None or app.status == "Offer":
            continue
        s = suggestion_for(row, app, db)
        if s is None or s.key not in _AUTO_KEYS[row.kind]:
            continue
        company = app.posting.company_name_raw or "an application"
        try:
            accept(db, row.id)
        except EmailSyncError as e:
            log_activity(db, f"Email check: could not update application {app.id} from an email: {e}", "WARNING")
            continue
        row = db.get(EmailMessage, row.id)
        if row.kind == "rejection":
            people = _contacts_for(db, app.id)
            row.action_taken = AUTO_REJECT_LABEL + (f"; you contacted {', '.join(people)[:120]}" if people else "")
            msg = (f"Marked {company} ({app.id}) as Not Selected from the employer's email of {row.received_at:%Y-%m-%d}."
                   + (f" People you contacted there, who could be asked for feedback: {', '.join(people)}." if people else ""))
        else:
            row.action_taken = AUTO_LABEL
            msg = f"Marked {company} ({app.id}) as Applied from the employer's confirmation email of {row.received_at:%Y-%m-%d}."
        db.commit()
        log_activity(db, msg, "INFO")
        done += 1
    return done


def _link_by_job_ref(db: Session, rows: list[EmailMessage]) -> None:
    """Amazon sends "Keep track of your application" with only the job ID; another email with the same ID from the
    same sender domain that did match tells which application it is."""
    for row in rows:
        if row.match_state == "matched" or not row.job_ref or row.kind == "ignore":
            continue
        domain = row.sender_email.rsplit("@", 1)[-1]
        sibling = (db.query(EmailMessage).filter(EmailMessage.job_ref == row.job_ref, EmailMessage.id != row.id,
                                                 EmailMessage.match_state == "matched",
                                                 EmailMessage.application_id.isnot(None),
                                                 EmailMessage.sender_email.like(f"%@{domain}"))
                   .order_by(EmailMessage.received_at).first())
        if sibling:
            row.match_state, row.application_id = "matched", sibling.application_id
            row.match_how = f"same job ID {row.job_ref} as another email from {domain}"


def _settle_if_nothing_to_do(db: Session, row: EmailMessage) -> None:
    """A matched email that would change nothing (a confirmation for an application already marked Applied) is
    recorded as noted, so the inbox only lists what needs a decision. The same for an email that could be about
    several applications when none of them would change (a confirmation while every candidate is already Applied)."""
    if row.state == "pending" and row.match_state == "choose" and row.candidate_ids:
        apps = [a for a in (db.get(JobApplication, i) for i in row.candidate_ids) if a is not None]
        if apps and all(suggestion_for(row, a, db) is None for a in apps):
            row.state, row.resolved_at = "noted", utcnow()
            row.action_taken = "nothing to change for any of this company's applications"
        return
    if row.state != "pending" or row.match_state != "matched":
        return
    app = db.get(JobApplication, row.application_id) if row.application_id else None
    if app is None:
        row.match_state, row.application_id = "unmatched", None
        return
    s = suggestion_for(row, app, db)
    if s is None:
        row.state, row.action_taken, row.resolved_at = "noted", f"nothing to change (already {app.status})", utcnow()


_RUN_LOCK = threading.Lock()  # the scheduler tick and "Check now" never read the mailbox at the same time


def is_running() -> bool:
    return _RUN_LOCK.locked()


def run_sync(db: Session, connect=None, force: bool = False) -> dict:
    """One read of the mailbox. `force` runs it even when the setting is off (the "Check now" button)."""
    if is_showcase_mode():
        raise EmailSyncError("The public demo has no mailbox; its inbox shows sample emails.")
    if not _RUN_LOCK.acquire(blocking=False):
        return {"skipped": "a check is already running"}
    try:
        return _run_sync(db, connect, force)
    finally:
        _RUN_LOCK.release()


def _run_sync(db: Session, connect, force: bool) -> dict:
    settings = get_or_create_settings(db)
    if not (settings.email_sync_enabled or force):
        return {"skipped": "email reading is switched off"}
    if not email_reader_service.is_configured():
        raise EmailSyncError("Email reading isn't set up: IMAP_HOST, IMAP_USER and IMAP_PASSWORD are needed in .env.")
    index = email_matcher.CompanyIndex(db)
    people = email_matcher.known_people(db)
    lookback = settings.email_sync_lookback_days or 60
    try:
        emails, highest, validity = email_reader_service.fetch_new(
            settings.email_sync_last_uid, date.today() - timedelta(days=lookback), settings.email_sync_uidvalidity,
            _wants(email_reader_service.mailbox_owner(), index, people), connect=connect)
    except EmailReaderError as e:
        settings.email_sync_last_error = str(e)[:300]
        settings.email_sync_last_run_at = utcnow()
        db.commit()
        raise EmailSyncError(str(e)) from e
    try:
        counts = ingest(db, emails)
    except IntegrityError:
        # Another process stored the same message first (a second app worker); the next run picks up the rest.
        db.rollback()
        raise EmailSyncError("Another check stored these emails at the same time; nothing was lost. Try again.")
    settings = get_or_create_settings(db)
    settings.email_sync_last_uid, settings.email_sync_uidvalidity = highest or settings.email_sync_last_uid, validity or None
    settings.email_sync_last_run_at, settings.email_sync_last_error = utcnow(), None
    db.commit()
    if counts["suggested"]:
        log_activity(db, f"Email check: {counts['suggested']} job email{'s' if counts['suggested'] != 1 else ''} to review "
                         f"on the inbox page ({counts['read']} read"
                         + (f", {counts['auto_applied']} applications updated from confirmations and rejections"
                            if counts.get("auto_applied") else "")
                         + ").", "INFO")
    return counts


def run_in_background() -> bool:
    """"Check now": the first read covers the whole look-back window and can take a few minutes, longer than a web
    request should wait, so it runs on its own thread with its own session. False when a check is already running."""
    if is_running():
        return False
    from ..database import SessionLocal

    def work():
        db = SessionLocal()
        try:
            counts = run_sync(db, force=True)
            if "skipped" not in counts and not counts.get("suggested"):
                log_activity(db, f"Email check: {counts.get('read', 0)} read, nothing new to review.", "INFO")
        except EmailSyncError as e:
            log_activity(db, f"Email check failed: {e}", "WARNING")
        except Exception as e:  # noqa: BLE001 -- recorded on the page instead of dying silently in a thread
            db.rollback()
            settings = get_or_create_settings(db)
            settings.email_sync_last_error, settings.email_sync_last_run_at = f"Unexpected error: {e}"[:300], utcnow()
            db.commit()
        finally:
            db.close()

    threading.Thread(target=work, daemon=True, name="email-check").start()
    return True


def run_if_enabled(db: Session) -> None:
    """Scheduler entry point: quiet when switched off or in the demo; a mailbox error is recorded, not raised."""
    if is_showcase_mode():
        return
    settings = get_or_create_settings(db)
    if not settings.email_sync_enabled:
        return
    try:
        run_sync(db)
    except EmailSyncError as e:
        log_activity(db, f"Email check failed: {e}", "WARNING")


# ---------------------------------------------------------------------------- what to do with a matched email

def _step(field: str, fn) -> tuple[str, Callable]:
    return field, fn


def suggestion_for(row: EmailMessage, app: JobApplication, db: Session | None = None) -> Suggestion | None:
    """The status change this email calls for on this application, or None when it calls for nothing."""
    status, kind = app.status, row.kind
    pre = status in email_matcher.PRE_APPLIED or status == "Rejected"
    applied = _step("applied_at", confirmation_service.mark_applied_from_email)
    closed_at = app.not_selected_at if status == "Not Selected" else app.offer_at if status == "Offer" else None
    # An interview email from before the rejection is history, not news; only one after it is worth a look.
    late = "This application is closed in the CRM; check whether it should be reopened." \
        if status in ("Not Selected", "Rejected") and not (closed_at and row.received_at and row.received_at <= closed_at) \
        else None
    if kind == "confirmation":
        return Suggestion("applied", "Mark as Applied", [applied]) if pre else None
    if kind == "rejection":
        if status in email_matcher.SENT:
            return Suggestion("not_selected", "Mark as Not Selected", [_step("not_selected_at", confirmation_service.mark_not_selected)],
                              "The CRM has this as Interviewing." if status == "Interviewing" else None)
        if pre:
            return Suggestion("applied_not_selected", "Mark as Applied, then Not Selected",
                              [applied, _step("not_selected_at", confirmation_service.mark_not_selected)])
        return None
    if kind == "interview":
        if status == "Applied":
            return Suggestion("interviewing", "Mark as Interviewing", [_step("interviewing_at", confirmation_service.mark_interviewing)])
        if pre and status != "Rejected":
            return Suggestion("applied_interviewing", "Mark as Applied, then Interviewing",
                              [applied, _step("interviewing_at", confirmation_service.mark_interviewing)])
        return Suggestion("check", "Check this one", [], late) if late else None
    if kind in ("assessment", "reply"):
        if status in email_matcher.SENT:
            return Suggestion("replied", "Record the reply", [_step("replied_at", confirmation_service.mark_replied)]) \
                if app.replied_at is None else None
        if pre and status != "Rejected":
            return Suggestion("applied_replied", "Mark as Applied and record the reply",
                              [applied, _step("replied_at", confirmation_service.mark_replied)])
        return Suggestion("check", "Check this one", [], late) if late else None
    if kind == "offer":
        if status in email_matcher.SENT:
            return Suggestion("offer", "Mark as Offer", [_step("offer_at", confirmation_service.mark_offer)])
        if pre:
            return Suggestion("applied_offer", "Mark as Applied, then Offer",
                              [applied, _step("offer_at", confirmation_service.mark_offer)])
        return Suggestion("check", "Check this one", [], late) if late else None
    return None


def _applied_date(db: Session, row: EmailMessage, app: JobApplication) -> datetime:
    """When the application went out: the earliest confirmation email tied to it, else this email's date."""
    first = (db.query(EmailMessage).filter(EmailMessage.application_id == app.id, EmailMessage.kind == "confirmation")
             .order_by(EmailMessage.received_at).first())
    return min(first.received_at, row.received_at) if first else row.received_at


def accept(db: Session, email_id: int, application_id: int | None = None) -> EmailMessage:
    """Apply the suggested change for one email (to `application_id` when the user picked it from several)."""
    row = db.get(EmailMessage, email_id)
    if row is None:
        raise EmailSyncError("That email is no longer in the inbox.")
    if row.state not in ("pending",):
        raise EmailSyncError("That email was already handled.")
    app_id = application_id or row.application_id
    app = db.get(JobApplication, app_id) if app_id else None
    if app is None:
        raise EmailSyncError("Pick the application this email is about first.")
    row.application_id, row.match_state = app.id, "matched"
    if application_id:
        row.match_how = "picked by you"
    s = suggestion_for(row, app, db)
    if s is None or not s.steps:
        row.state, row.action_taken, row.resolved_at = "noted", (s.label if s else f"nothing to change (already {app.status})"), utcnow()
        db.commit()
        return row
    try:
        for field, fn in s.steps:
            at = _applied_date(db, row, app) if field == "applied_at" else row.received_at
            fn(db, app.id, at=at)
            db.refresh(app)
    except ConfirmationServiceError as e:
        db.rollback()
        raise EmailSyncError(str(e)) from e
    row = db.get(EmailMessage, email_id)
    row.state, row.action_taken, row.resolved_at = "done", s.label, utcnow()
    db.commit()
    # Other waiting emails about the same application may now need nothing (a confirmation after the rejection).
    for other in db.query(EmailMessage).filter(EmailMessage.application_id == app.id, EmailMessage.state == "pending",
                                               EmailMessage.id != row.id).all():
        _settle_if_nothing_to_do(db, other)
    db.commit()
    return row


def accept_many(db: Session, email_ids: list[int]) -> tuple[int, list[str]]:
    done, errors = 0, []
    for email_id in email_ids:
        row = db.get(EmailMessage, email_id)
        if row is not None and row.state == "noted":
            continue  # a ticked duplicate that an earlier one in this batch already settled
        try:
            accept(db, email_id)
            done += 1
        except EmailSyncError as e:
            errors.append(f"#{email_id}: {e}")
    return done, errors


def dismiss(db: Session, email_id: int) -> EmailMessage:
    row = db.get(EmailMessage, email_id)
    if row is None:
        raise EmailSyncError("That email is no longer in the inbox.")
    row.state, row.action_taken, row.resolved_at = "dismissed", "dismissed", utcnow()
    db.commit()
    return row


def restore(db: Session, email_id: int) -> EmailMessage:
    """Put a dismissed or ignored email back in the queue (the rules got it wrong)."""
    row = db.get(EmailMessage, email_id)
    if row is None:
        raise EmailSyncError("That email is no longer in the inbox.")
    if row.state not in ("dismissed", "ignored", "noted"):
        raise EmailSyncError("Only a dismissed, ignored or noted email can be put back.")
    row.state, row.action_taken, row.resolved_at = "pending", None, None
    if row.kind == "ignore":
        row.kind = "reply"  # the user says it matters; treat it as a person's reply unless they pick otherwise
    db.commit()
    return row


def set_kind(db: Session, email_id: int, kind: str) -> EmailMessage:
    """The user corrects what the rules decided (a rejection read as a confirmation, say)."""
    if kind not in KIND_LABELS or kind == "ignore":
        raise EmailSyncError("Unknown email type.")
    row = db.get(EmailMessage, email_id)
    if row is None or row.state != "pending":
        raise EmailSyncError("Only a waiting email can be changed.")
    row.kind, row.confidence, row.reason = kind, "high", "set by you"
    db.commit()
    return row


def add_to_crm(db: Session, email_id: int, company: str, role: str) -> JobApplication:
    """An email about an application the CRM never had (applied straight on the employer's site): create the company,
    a posting marked source="email" and the application, then apply the email's change to it."""
    row = db.get(EmailMessage, email_id)
    if row is None or row.state != "pending":
        raise EmailSyncError("That email was already handled.")
    company, role = (company or "").strip(), (role or "").strip()
    if not company:
        raise EmailSyncError("Give the company name.")
    role = role or "Role named in the email"
    company_row = get_or_create_company(db, company)
    posting = JobPosting(
        company_id=company_row.id, company_name_raw=company, job_title=role[:200], job_url=None,
        job_description=f"Added from the employer's email of {row.received_at:%Y-%m-%d}: {row.subject or ''}".strip(),
        source="email", external_id=row.job_ref, first_seen_at=row.received_at, last_seen_at=row.received_at,
    )
    db.add(posting)
    db.flush()
    app = JobApplication(posting_id=posting.id, status="Ingested", created_at=row.received_at)
    db.add(app)
    db.commit()
    log_activity(db, f"Added '{role}' at {company} from an email ({KIND_LABELS.get(row.kind, row.kind).lower()}).", "INFO")
    row.match_state, row.application_id, row.match_how = "matched", app.id, "added from this email"
    db.commit()
    accept(db, email_id, app.id)
    rematch_pending(db)
    return app


def rematch_pending(db: Session) -> int:
    """Run matching again for waiting emails with no clear application (after one was added, say)."""
    index = email_matcher.CompanyIndex(db)
    people = email_matcher.known_people(db)
    changed = 0
    for row in db.query(EmailMessage).filter(EmailMessage.state == "pending", EmailMessage.match_state != "matched").all():
        raw = RawEmail(uid=0, message_id=row.message_id, sender_name=row.sender_name or "", sender_email=row.sender_email,
                       subject=row.subject or "", received_at=row.received_at, text=row.excerpt or "")
        cls = email_classifier.Classification(row.kind, row.confidence, row.reason or "", company=row.company_guess,
                                              role=email_classifier._tidy_role(row.role_guess), job_ref=row.job_ref,
                                              hints=email_classifier.sender_hints(raw))
        result = email_matcher.match(index, people, raw, cls)
        if result.state != row.match_state or result.application_id != row.application_id:
            row.match_state, row.application_id = result.state, result.application_id
            row.candidate_ids, row.match_how = result.candidate_ids or None, result.how[:300]
            changed += 1
            _settle_if_nothing_to_do(db, row)
    db.commit()
    return changed


# ---------------------------------------------------------------------------- reading it back

def feedback_draft(row: EmailMessage, app: JobApplication | None, name: str | None) -> str | None:
    """A short ask for feedback after a no from a person (a recruiter), for the user to send themselves. None for
    no-reply senders, who cannot answer."""
    if row.kind != "rejection" or not email_classifier.is_person(
            RawEmail(0, "", row.sender_name or "", row.sender_email, "", row.received_at)):
        return None
    first = (row.sender_name or "").split(" ")[0] or "there"
    role = app.posting.job_title if app else (row.role_guess or "the role")
    return (f"Hi {first},\n\nThank you for letting me know, and for the time you gave me on the {role} process. "
            f"If you have a minute, I'd value any feedback on where my background fell short for this role, so I can "
            f"work on it. I'd also be glad to stay in touch if something closer to my experience opens up.\n\n"
            f"Thank you,\n{name or ''}").strip()


def inbox(db: Session) -> dict:
    rows = db.query(EmailMessage).order_by(EmailMessage.received_at.desc()).all()
    apps = {a.id: a for a in db.query(JobApplication).filter(
        JobApplication.id.in_({i for r in rows for i in ([r.application_id] if r.application_id else []) + (r.candidate_ids or [])})).all()} \
        if rows else {}
    groups = {"suggested": [], "choose": [], "unmatched": [], "handled": [], "ignored": []}
    for r in rows:
        app = apps.get(r.application_id)
        item = {"row": r, "app": app, "label": KIND_LABELS.get(r.kind, r.kind),
                "candidates": [apps[i] for i in (r.candidate_ids or []) if i in apps],
                "link": email_reader_service.gmail_link(r.thread_id)}
        if r.state == "pending":
            if r.match_state == "matched" and app is not None:
                item["suggestion"] = suggestion_for(r, app, db)
                groups["suggested"].append(item)
            elif r.match_state == "choose" and item["candidates"]:
                groups["choose"].append(item)
            else:
                groups["unmatched"].append(item)
        elif r.state == "ignored":
            if len(groups["ignored"]) < 100:
                groups["ignored"].append(item)
        elif len(groups["handled"]) < 150:
            groups["handled"].append(item)
    return groups


def pending_count(db: Session) -> int:
    return db.query(EmailMessage).filter(EmailMessage.state == "pending").count()


def emails_for_application(db: Session, application_id: int) -> list[dict]:
    rows = (db.query(EmailMessage).filter(EmailMessage.application_id == application_id, EmailMessage.kind != "ignore")
            .order_by(EmailMessage.received_at.desc()).all())
    return [{"row": r, "label": KIND_LABELS.get(r.kind, r.kind), "link": email_reader_service.gmail_link(r.thread_id)}
            for r in rows]
