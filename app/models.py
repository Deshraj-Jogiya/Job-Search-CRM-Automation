"""
Data model for the job-search CRM/automation platform.

Designed against the full roadmap up front so later phases (confirmation
queue, outreach, interview prep, analytics) don't require bolt-on schema
surgery. Not every column is used by the code that exists yet -- that's
intentional; it means each phase just fills in behavior against a schema
that already fits, instead of migrating tables mid-project.
"""

from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float
)
from sqlalchemy.orm import relationship
from .database import Base


# ---------------------------------------------------------------------------
# Profile (Phase 1: living profile, versioned, multi-variant)
# ---------------------------------------------------------------------------

class ProfileVariant(Base):
    """A named 'flavor' of your base profile (e.g. Data Engineering, ML
    Engineering, Analytics). Tailoring starts from whichever variant is
    the closest fit for a given job, instead of stretching one master
    resume in every direction."""
    __tablename__ = "profile_variants"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)  # e.g. "Data Engineering"
    is_default = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    versions = relationship("ProfileVersion", back_populates="variant", cascade="all, delete-orphan")


class ProfileVersion(Base):
    """Versioned snapshot of a profile variant's content. New versions are
    created when the profile changes (portfolio sync, LinkedIn paste-diff,
    manual edit) -- never overwritten in place, so tailoring history stays
    traceable to what your profile actually said at the time."""
    __tablename__ = "profile_versions"

    id = Column(Integer, primary_key=True, index=True)
    variant_id = Column(Integer, ForeignKey("profile_variants.id"), nullable=False)
    content_json = Column(Text, nullable=False)  # full structured resume data
    source = Column(String, default="manual")  # 'portfolio_sync' | 'linkedin_diff' | 'manual'
    change_summary = Column(Text, nullable=True)  # human-readable diff summary, AI-generated
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    variant = relationship("ProfileVariant", back_populates="versions")


# ---------------------------------------------------------------------------
# Companies (Phase 2: company memory / block-deprioritize list)
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, index=True)
    normalized_name = Column(String, nullable=False, index=True)  # lowercased, suffixes stripped, for matching
    status = Column(String, default="Neutral")  # 'Neutral' | 'Deprioritized' | 'Blocked'
    status_reason = Column(String, nullable=True)  # e.g. "Ghosted after application", "Not interested"
    ghosted_count = Column(Integer, default=0)  # applications that went silent
    created_at = Column(DateTime, default=datetime.utcnow)

    # Phase 2 slice 2: direct ATS board polling. Auto-detected (see
    # board_discovery.py) the first time this company is seen via any
    # source, or manually set/overridden from the Jobs page -- null
    # means "no board found/configured on that ATS," not "not checked
    # yet" (see board_slugs_checked_at for that distinction).
    greenhouse_slug = Column(String, nullable=True)
    lever_slug = Column(String, nullable=True)
    ashby_slug = Column(String, nullable=True)
    board_slugs_checked_at = Column(DateTime, nullable=True)

    postings = relationship("JobPosting", back_populates="company")


# ---------------------------------------------------------------------------
# Job postings & applications (Phase 2-4)
# ---------------------------------------------------------------------------

class JobPosting(Base):
    """A distinct job posting as seen from a source. Multiple JobPosting
    rows (across sources, or across reposts over time) can point to the
    same logical job -- repost/staleness detection lives here."""
    __tablename__ = "job_postings"

    id = Column(Integer, primary_key=True, index=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)
    company_name_raw = Column(String, nullable=False)  # as scraped, before normalization
    job_title = Column(String, nullable=False, index=True)
    job_url = Column(String, nullable=True)
    job_description = Column(Text, nullable=False)
    source = Column(String, nullable=False)  # 'linkedin' | 'adzuna' | 'greenhouse' | 'lever' | 'ashby' | 'manual'
    external_id = Column(String, nullable=True, index=True)  # source's own posting id, when available

    first_seen_at = Column(DateTime, default=datetime.utcnow)  # earliest time this exact posting was observed
    last_seen_at = Column(DateTime, default=datetime.utcnow)   # most recent time it was still live
    repost_count = Column(Integer, default=0)

    # Scam/ghost-job signals -- surfaced, never silently filtered
    scam_flag_reason = Column(String, nullable=True)
    staleness_flag = Column(Boolean, default=False)

    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="postings")
    application = relationship("JobApplication", back_populates="posting", uselist=False, cascade="all, delete-orphan")


class JobApplication(Base):
    """The application lifecycle for a given posting. One-to-one with
    JobPosting -- separated so posting metadata (source, scam flags) stays
    independent of application-state (status, confirmation window, score).
    """
    __tablename__ = "job_applications"

    id = Column(Integer, primary_key=True, index=True)
    posting_id = Column(Integer, ForeignKey("job_postings.id"), nullable=False, unique=True)

    match_score = Column(Integer, default=0)
    match_analysis_json = Column(Text, nullable=True)
    cover_letter_score = Column(Integer, nullable=True)

    profile_variant_id = Column(Integer, ForeignKey("profile_variants.id"), nullable=True)

    status = Column(String, default="Ingested")
    # Ingested -> Tailored -> Pending Confirmation -> Applied -> Interviewing -> Offer
    #                                                          -> Needs Review
    #          -> Rejected (retained briefly, then swept)

    # Confirmation queue (Phase 4)
    confirmation_deadline = Column(DateTime, nullable=True)
    confirmed_by_user = Column(Boolean, default=False)
    notification_sent = Column(Boolean, default=False)  # individual (fast-track) or included in a digest yet?

    visa_sponsorship = Column(String, default="Unknown")
    recruiter_name = Column(String, nullable=True)
    recruiter_linkedin = Column(String, nullable=True)
    recruiter_email = Column(String, nullable=True)

    applied_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)

    notes = Column(Text, nullable=True)
    attention_reason = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    posting = relationship("JobPosting", back_populates="application")
    documents = relationship("TailoredDocument", back_populates="application", cascade="all, delete-orphan")
    outreach_messages = relationship("OutreachMessage", back_populates="application", cascade="all, delete-orphan")
    interview_prep = relationship("InterviewPrep", back_populates="application", uselist=False, cascade="all, delete-orphan")


class TailoredDocument(Base):
    __tablename__ = "tailored_documents"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False)
    document_type = Column(String, nullable=False)  # 'resume' | 'cover_letter'
    content = Column(Text, nullable=False)
    ats_score = Column(Integer, nullable=True)
    generated_at = Column(DateTime, default=datetime.utcnow)

    application = relationship("JobApplication", back_populates="documents")


# ---------------------------------------------------------------------------
# Outreach (Phase 5: review-gated, capped)
# ---------------------------------------------------------------------------

class OutreachMessage(Base):
    __tablename__ = "outreach_messages"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False)

    channel = Column(String, default="email")  # 'email' | 'linkedin_connection' | 'linkedin_inmail'
    recipient_name = Column(String, nullable=True)
    recipient_address = Column(String, nullable=True)  # email, or LinkedIn URL
    subject = Column(String, nullable=True)
    body = Column(Text, nullable=False)

    status = Column(String, default="Draft")  # 'Draft' -> 'Pending Confirmation' -> 'Approved' -> 'Sent' | 'Rejected'
    confirmation_deadline = Column(DateTime, nullable=True)

    email_verified = Column(Boolean, default=False)  # syntax + MX check passed
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    application = relationship("JobApplication", back_populates="outreach_messages")


# ---------------------------------------------------------------------------
# Interview prep (Phase 6)
# ---------------------------------------------------------------------------

class InterviewPrep(Base):
    __tablename__ = "interview_prep"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False, unique=True)

    general_prep_json = Column(Text, nullable=True)   # questions/talking points based on your background
    company_prep_json = Column(Text, nullable=True)   # company-specific angles, from JD + light research
    generated_at = Column(DateTime, default=datetime.utcnow)

    application = relationship("JobApplication", back_populates="interview_prep")


# ---------------------------------------------------------------------------
# Search configuration
# ---------------------------------------------------------------------------

class SearchKeyword(Base):
    __tablename__ = "search_keywords"

    id = Column(Integer, primary_key=True, index=True)
    keyword = Column(String, nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True)


class JobSource(Base):
    """Which intake sources are enabled, and their own health/quota state
    -- so we can respect each free-tier API budget independently instead
    of one global polling number."""
    __tablename__ = "job_sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)  # 'linkedin' | 'adzuna' | 'greenhouse' | 'lever' | 'ashby'
    is_active = Column(Boolean, default=True)
    calls_used_this_period = Column(Integer, default=0)
    period_reset_at = Column(DateTime, nullable=True)
    last_polled_at = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)


# ---------------------------------------------------------------------------
# Global settings (every tunable number, live-editable)
# ---------------------------------------------------------------------------

class GlobalSettings(Base):
    __tablename__ = "global_settings"

    id = Column(Integer, primary_key=True, index=True)

    # Kill switch
    automation_enabled = Column(Boolean, default=True)

    # Phase 2: intake cadence
    fast_poll_interval_minutes = Column(Integer, default=10)   # cheap "anything new?" check
    full_ingest_interval_minutes = Column(Integer, default=15)  # full scoring/tailoring pass
    stale_posting_threshold_days = Column(Integer, default=45)  # flag postings open longer than this (warning only)

    # Phase 4: confirmation queue
    confirmation_window_hours = Column(Float, default=15.0)
    fast_track_score_threshold = Column(Integer, default=90)   # very high match...
    fast_track_freshness_minutes = Column(Integer, default=30)  # ...and very fresh -> shrink the window
    fast_track_window_hours = Column(Float, default=2.0)
    rejected_retention_days = Column(Integer, default=7)

    # Phase 4: quiet hours -- a confirmation deadline that would land inside
    # this daily local-time window gets pushed to the end of it, so it
    # never silently lapses while the user is predictably unreachable
    # (e.g. asleep). Generic on purpose -- not specific to one schedule.
    quiet_hours_enabled = Column(Boolean, default=True)
    quiet_hours_start_hour = Column(Integer, default=23)  # local 24h clock
    quiet_hours_end_hour = Column(Integer, default=7)
    local_timezone = Column(String, default="America/Phoenix")  # IANA tz name

    # Phase 4: notification digest -- individual emails are reserved for
    # fast-track only; everything else batches into one periodic digest
    # so queueing many applications at once can't spam the inbox.
    notification_digest_interval_minutes = Column(Integer, default=30)
    last_digest_sent_at = Column(DateTime, nullable=True)

    # Phase 5: outreach
    daily_outreach_cap = Column(Integer, default=10)

    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def get_or_create_settings(db) -> "GlobalSettings":
    settings = db.query(GlobalSettings).first()
    if not settings:
        settings = GlobalSettings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

class ActivityLog(Base):
    __tablename__ = "activity_logs"

    id = Column(Integer, primary_key=True, index=True)
    message = Column(Text, nullable=False)
    level = Column(String, default="INFO")
    timestamp = Column(DateTime, default=datetime.utcnow)
