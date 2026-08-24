"""
Phase 4: the confirmation-gated queue. An application that finishes
tailoring lands here -- into Needs Review if Phase 2/3 flagged something
serious (no timeout, always waits for an explicit human decision), into
a timed Pending Confirmation queue (clean but no real autofill support
for its source), or -- for a clean, autofill-supported application --
straight to Approved with a real browser auto-launched immediately (see
the auto-launch design note in CLAUDE.md's Phase 4 section). See
CLAUDE.md's "Phase 4" section for the full settled design this
implements.

Nothing here does real portal submission -- there is no such engine in
this rebuild. "Approved" means tailored documents are ready and the
human has (explicitly, by not objecting within the window, or via the
auto-launch path above) cleared this to go out; a separate explicit
"Mark as Applied" action records that the human actually submitted it
somewhere.
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import GlobalSettings, JobApplication, TailoredDocument, get_or_create_settings
from .activity_logger import log_activity


class ConfirmationServiceError(Exception):
    """User-facing failure -- callers show the message instead of a 500."""


def _hour_in_range(hour: int, start: int, end: int) -> bool:
    """True if `hour` falls within [start, end), handling the overnight
    wraparound case (e.g. start=23, end=7 means 23,0,1,...,6)."""
    if start == end:
        return False  # a zero-width window never applies
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def _push_past_quiet_hours(deadline_utc: datetime, settings: GlobalSettings) -> datetime:
    try:
        tz = ZoneInfo(settings.local_timezone)
    except Exception:
        return deadline_utc  # bad tz config shouldn't break the whole queue

    local_deadline = deadline_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    if not _hour_in_range(local_deadline.hour, settings.quiet_hours_start_hour, settings.quiet_hours_end_hour):
        return deadline_utc

    pushed = local_deadline.replace(hour=settings.quiet_hours_end_hour, minute=0, second=0, microsecond=0)
    if pushed <= local_deadline:
        pushed += timedelta(days=1)
    return pushed.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


def compute_confirmation_deadline(settings: GlobalSettings, application: JobApplication, now: datetime) -> tuple[datetime, bool]:
    """Returns (deadline, is_fast_track)."""
    posting = application.posting
    is_fast_track = (
        application.match_score is not None
        and application.match_score >= settings.fast_track_score_threshold
        and posting.first_seen_at is not None
        and posting.first_seen_at >= now - timedelta(minutes=settings.fast_track_freshness_minutes)
    )
    window_hours = settings.fast_track_window_hours if is_fast_track else settings.confirmation_window_hours
    deadline = now + timedelta(hours=window_hours)

    if settings.quiet_hours_enabled:
        deadline = _push_past_quiet_hours(deadline, settings)

    return deadline, is_fast_track


def has_hard_stop_flag(application: JobApplication) -> str | None:
    """Returns a human-readable reason if this application must always
    wait for an explicit human decision, regardless of any timeout --
    or None if it's safe to fast-track/auto-proceed. Both signals are
    already-computed warnings from earlier phases (tailoring fabrication
    check, intake scam-pattern check) -- this just decides what they
    mean for the confirmation queue."""
    if application.attention_reason:
        return application.attention_reason
    if application.posting.scam_flag_reason:
        return f"Scam-pattern warning on the posting: {application.posting.scam_flag_reason}"
    if application.posting.eligibility_flag_reason:
        return f"Eligibility requirement stated in the posting: {application.posting.eligibility_flag_reason}"
    return None


def evaluate_and_enqueue(db: Session, application_id: int) -> JobApplication:
    """Call once tailoring succeeds. Routes to Needs Review (flagged, no
    timeout), straight to Approved + an auto-launched real browser
    (clean AND autofill-supported -- see the Phase 4 auto-launch design
    note in CLAUDE.md), or Pending Confirmation (clean but not
    autofill-supported, timed).

    Queueing many applications at once must not mean an email per
    application -- only fast-track (rare, speed-critical) gets an
    immediate individual email. Everything else is left with
    notification_sent=False and picked up by the next periodic digest
    (see notification_service.send_digest()), which points at the bulk
    review page instead of listing every item inline."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")

    settings = get_or_create_settings(db)
    hard_stop_reason = has_hard_stop_flag(application)
    is_fast_track = False

    # Re-entering the queue (e.g. re-tailored after an earlier digest
    # already covered it) must be visible to the NEXT digest/fast-track
    # email too, not permanently invisible because it was notified once
    # before under a since-resolved state.
    application.notification_sent = False

    from . import autofill_service

    if hard_stop_reason:
        application.status = "Needs Review"
        application.confirmation_deadline = None
        db.commit()
        log_activity(
            db,
            f"'{application.posting.job_title}' at {application.posting.company_name_raw} needs manual review "
            f"before it can proceed: {hard_stop_reason}",
            "WARNING",
        )
    elif (
        autofill_service.is_supported(application.posting.source)
        and application.match_score >= settings.min_score_for_auto_launch
    ):
        # A clean (no hard-stop flag), autofill-supported, AND
        # sufficiently-well-matched application skips the timed Pending
        # Confirmation step entirely -- the human's own click on the
        # real submit button in the auto-launched browser is the
        # confirmation, no other approval gate for this path. This is
        # actually safer than the timeout-based auto-proceed below,
        # which can flip an application to Approved with zero human
        # action if the user is unreachable -- nothing here ever
        # becomes Applied without a genuine human submit click on the
        # real site, since the automation never clicks submit itself
        # (see autofill_service/autofill/* docstrings). The match_score
        # gate exists because being clean/unflagged is not the same as
        # being a good fit -- a low-scoring application that happens
        # not to trigger the fabrication check would otherwise still
        # pop open a real, unattended browser for a job the candidate
        # is a poor match for. Below the bar, it still falls through to
        # the ordinary timed Pending Confirmation branch, same as any
        # other clean application on a non-autofill source.
        application.status = "Approved"
        application.confirmed_by_user = False  # no explicit UI click -- auto-proceeded, same convention as the timeout path below
        application.confirmation_deadline = None
        db.commit()
        log_activity(
            db,
            f"'{application.posting.job_title}' at {application.posting.company_name_raw} tailored clean -- "
            "opening a real browser now to pre-fill the application for your review.",
            "INFO",
        )
        autofill_service.launch_autofill_in_background(application.id)

        from . import notification_service

        application.notification_sent = notification_service.send_autofill_ready_notification(db, application)
        db.commit()
    else:
        now = utcnow()
        deadline, is_fast_track = compute_confirmation_deadline(settings, application, now)
        application.status = "Pending Confirmation"
        application.confirmation_deadline = deadline
        application.confirmed_by_user = False
        db.commit()
        log_activity(
            db,
            f"'{application.posting.job_title}' at {application.posting.company_name_raw} entered the "
            f"confirmation queue ({'fast-track' if is_fast_track else 'standard'}, "
            f"deadline {deadline.strftime('%Y-%m-%d %H:%M')} UTC).",
            "INFO",
        )

    if is_fast_track:
        from . import notification_service
        if notification_service.send_confirmation_notification(db, application):
            application.notification_sent = True
            db.commit()
    # else: leave notification_sent=False -- the digest sweep picks it up.

    return application


def approve_application(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")
    if application.status not in ("Pending Confirmation", "Needs Review"):
        raise ConfirmationServiceError(f"Application is '{application.status}', not awaiting confirmation.")

    application.status = "Approved"
    application.confirmed_by_user = True
    application.confirmation_deadline = None
    db.commit()
    log_activity(db, f"Approved '{application.posting.job_title}' at {application.posting.company_name_raw}.", "INFO")
    return application


def reject_application(db: Session, application_id: int) -> JobApplication:
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")
    if application.status not in ("Pending Confirmation", "Needs Review"):
        raise ConfirmationServiceError(f"Application is '{application.status}', not awaiting confirmation.")

    application.status = "Rejected"
    application.rejected_at = utcnow()
    application.confirmation_deadline = None
    db.commit()
    log_activity(db, f"Rejected '{application.posting.job_title}' at {application.posting.company_name_raw}.", "INFO")
    return application


def mark_applied(db: Session, application_id: int) -> JobApplication:
    """Confirmation that the application was actually submitted --
    either an explicit human click (the dashboard button), or, since
    Phase 17, autofill_service's own submission-confirmation watcher
    calling this after observing a real post-submit page signal. Either
    way this is the single source of truth for "submitted," not a
    guess."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")
    if application.status != "Approved":
        raise ConfirmationServiceError(f"Application is '{application.status}', not yet Approved.")

    application.status = "Applied"
    application.applied_at = utcnow()
    db.commit()
    log_activity(db, f"Marked '{application.posting.job_title}' at {application.posting.company_name_raw} as Applied.", "INFO")
    return application


def mark_interviewing(db: Session, application_id: int) -> JobApplication:
    """Phase 7: explicit self-report that an interview is happening --
    same trust model as mark_applied. Feeds the outcome-analytics
    funnel; nothing infers this automatically since there's no email-
    scanning integration in this build."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")
    if application.status != "Applied":
        raise ConfirmationServiceError(f"Application is '{application.status}', not yet Applied.")

    application.status = "Interviewing"
    application.interviewing_at = utcnow()
    db.commit()
    log_activity(db, f"Marked '{application.posting.job_title}' at {application.posting.company_name_raw} as Interviewing.", "INFO")
    return application


def mark_offer(db: Session, application_id: int) -> JobApplication:
    """Reachable from Applied directly too (not every offer goes through
    a tracked 'Interviewing' click first) -- both are real, valid paths."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")
    if application.status not in ("Applied", "Interviewing"):
        raise ConfirmationServiceError(f"Application is '{application.status}', not Applied or Interviewing.")

    application.status = "Offer"
    application.offer_at = utcnow()
    db.commit()
    log_activity(db, f"Marked '{application.posting.job_title}' at {application.posting.company_name_raw} as Offer.", "INFO")
    return application


def mark_not_selected(db: Session, application_id: int) -> JobApplication:
    """Distinct from reject_application()'s 'Rejected' -- this is a
    real applied-and-declined (or gone silent) outcome, worth keeping
    for analytics history, not swept/deleted like a pre-apply decline.
    Increments Company.ghosted_count as a simple, real signal for
    company memory -- covers both an explicit decline and silence
    without needing the user to distinguish which at click time."""
    application = db.query(JobApplication).filter(JobApplication.id == application_id).first()
    if not application:
        raise ConfirmationServiceError(f"Application {application_id} not found.")
    if application.status not in ("Applied", "Interviewing"):
        raise ConfirmationServiceError(f"Application is '{application.status}', not Applied or Interviewing.")

    application.status = "Not Selected"
    application.not_selected_at = utcnow()
    company = application.posting.company
    if company:
        company.ghosted_count = (company.ghosted_count or 0) + 1
    db.commit()
    log_activity(db, f"Marked '{application.posting.job_title}' at {application.posting.company_name_raw} as Not Selected.", "INFO")
    return application


def sweep_expired_confirmations(db: Session) -> int:
    """Called by the scheduler tick. Auto-proceeds any Pending
    Confirmation application whose deadline has passed -- Needs Review
    items are untouched here since they have no deadline by design."""
    now = utcnow()
    expired = (
        db.query(JobApplication)
        .filter(JobApplication.status == "Pending Confirmation", JobApplication.confirmation_deadline <= now)
        .all()
    )
    for application in expired:
        application.status = "Approved"
        application.confirmed_by_user = False  # auto-proceeded on timeout, not an explicit click
        application.confirmation_deadline = None
        log_activity(
            db,
            f"Confirmation window expired for '{application.posting.job_title}' at "
            f"{application.posting.company_name_raw} -- auto-approved (no objection within the window).",
            "INFO",
        )
    if expired:
        db.commit()
    return len(expired)


def sweep_rejected_retention(db: Session) -> int:
    """Hard-deletes Rejected applications older than
    rejected_retention_days, per CLAUDE.md -- cascades to
    TailoredDocument/OutreachMessage/InterviewPrep via the ORM
    relationship, but NOT the JobPosting (company memory / repost
    detection still wants that posting to have existed)."""
    settings = get_or_create_settings(db)
    cutoff = utcnow() - timedelta(days=settings.rejected_retention_days)
    old_rejected = (
        db.query(JobApplication)
        .filter(JobApplication.status == "Rejected", JobApplication.rejected_at <= cutoff)
        .all()
    )
    count = len(old_rejected)
    for application in old_rejected:
        db.delete(application)
    if old_rejected:
        db.commit()
        log_activity(db, f"Swept {count} rejected application(s) past the {settings.rejected_retention_days}-day retention window.", "INFO")
    return count
