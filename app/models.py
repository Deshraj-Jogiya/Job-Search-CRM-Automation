"""
Data model for the job-search CRM/automation platform.

Designed against the full roadmap up front so later phases (confirmation
queue, outreach, interview prep, analytics) don't require bolt-on schema
surgery. Not every column is used by the code that exists yet -- that's
intentional; it means each phase just fills in behavior against a schema
that already fits, instead of migrating tables mid-project.
"""

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float
)
from sqlalchemy.orm import relationship
from .app_mode import is_showcase_mode
from .database import Base, utcnow


# ---------------------------------------------------------------------------
# Operator account (real login/signup/password-reset)
# ---------------------------------------------------------------------------

class AdminAccount(Base):
    """The one operator account for THIS deployment. Deliberately not a
    multi-tenant User table -- this project's "public" model is one
    person forks/deploys their own instance with their own .env
    secrets and their own database, not many strangers sharing one
    deployment. This table replaces the old bare DASHBOARD_PASSWORD
    env-var comparison with a real signup/login/forgot-password flow;
    "signup" here means first-run setup (create the one account this
    instance will ever have), not open registration -- see
    app/routers/auth.py's guard against creating a second row.

    A deployment with zero rows here falls back to the legacy
    DASHBOARD_PASSWORD-env-var-or-open behavior (app/main.py's
    require_auth) so nothing breaks for an existing install that hasn't
    signed up yet."""
    __tablename__ = "admin_accounts"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String, nullable=False)
    password_hash = Column(String, nullable=False)
    recovery_email = Column(String, nullable=True)  # required for forgot-password to work
    created_at = Column(DateTime, default=utcnow)


# ---------------------------------------------------------------------------
# Profile (living, versioned, multi-variant)
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
    created_at = Column(DateTime, default=utcnow)

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
    created_at = Column(DateTime, default=utcnow)

    variant = relationship("ProfileVariant", back_populates="versions")


# ---------------------------------------------------------------------------
# Companies (company memory / block-deprioritize list)
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "companies"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True, index=True)
    normalized_name = Column(String, nullable=False, index=True)  # lowercased, suffixes stripped, for matching
    status = Column(String, default="Neutral")  # 'Neutral' | 'Deprioritized' | 'Blocked'
    status_reason = Column(String, nullable=True)  # e.g. "Ghosted after application", "Not interested"
    ghosted_count = Column(Integer, default=0)  # applications that went silent
    created_at = Column(DateTime, default=utcnow)

    # Direct ATS board polling. Auto-detected (see
    # board_discovery.py) the first time this company is seen via any
    # source, or manually set/overridden from the Jobs page -- null
    # means "no board found/configured on that ATS," not "not checked
    # yet" (see board_slugs_checked_at for that distinction).
    greenhouse_slug = Column(String, nullable=True)
    lever_slug = Column(String, nullable=True)
    ashby_slug = Column(String, nullable=True)
    recruitee_slug = Column(String, nullable=True)
    personio_slug = Column(String, nullable=True)  # bare slug only -- see personio_source.py for the .com/.de split
    board_slugs_checked_at = Column(DateTime, nullable=True)

    postings = relationship("JobPosting", back_populates="company")


# ---------------------------------------------------------------------------
# Job postings & applications
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
    location = Column(String, nullable=True)  # structured location text from the source's own API, when available
    job_description = Column(Text, nullable=False)
    source = Column(String, nullable=False)  # 'linkedin' | 'adzuna' | 'greenhouse' | 'lever' | 'ashby' | 'recruitee' | 'personio' | 'jobspipe' | 'manual'
    external_id = Column(String, nullable=True, index=True)  # source's own posting id, when available

    first_seen_at = Column(DateTime, default=utcnow)  # earliest time this exact posting was observed
    last_seen_at = Column(DateTime, default=utcnow)   # most recent time it was still live
    repost_count = Column(Integer, default=0)

    # Scam/ghost-job signals -- surfaced, never silently filtered
    scam_flag_reason = Column(String, nullable=True)
    staleness_flag = Column(Boolean, default=False)

    # Hard eligibility requirements mechanically detected in the JD text at
    # intake time (U.S. citizenship, active security clearance, HIPAA/PHI
    # handling authorization, etc.) -- surfaced as a warning, same as scam
    # flags, never a silent intake-time filter (whether a given requirement
    # actually excludes this candidate depends on personal facts this
    # project doesn't assume). confirmation_service.has_hard_stop_flag()
    # additionally treats this as a hard-stop so a flagged posting always
    # needs an explicit human look before ever auto-proceeding.
    eligibility_flag_reason = Column(String, nullable=True)

    created_at = Column(DateTime, default=utcnow)

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
    # Ingested -> Tailored -> Pending Confirmation -> Approved -> Applied -> Interviewing -> Offer
    #                                                           \          \-> Not Selected
    #                                                            -> Needs Review
    #          -> Rejected (declined before applying; retained briefly, then swept)
    #
    # Interviewing/Offer/Not Selected are manual self-reports via
    # confirmation_service.mark_interviewing/mark_offer/
    # mark_not_selected, same trust model as mark_applied -- nothing infers
    # these automatically. "Not Selected" is deliberately a different status
    # from "Rejected": "Rejected" means the user declined to apply and is
    # swept/deleted after rejected_retention_days (see sweep_rejected_
    # retention); "Not Selected" means the user DID apply and the outcome
    # was a decline or silence post-application -- real analytics history
    # worth keeping, not garbage to sweep.

    # Confirmation queue
    confirmation_deadline = Column(DateTime, nullable=True)
    confirmed_by_user = Column(Boolean, default=False)
    notification_sent = Column(Boolean, default=False)  # individual (fast-track) or included in a digest yet?

    visa_sponsorship = Column(String, default="Unknown")
    recruiter_name = Column(String, nullable=True)
    recruiter_linkedin = Column(String, nullable=True)
    recruiter_email = Column(String, nullable=True)

    applied_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    interviewing_at = Column(DateTime, nullable=True)  # only set by an explicit Mark as Interviewing click
    offer_at = Column(DateTime, nullable=True)
    not_selected_at = Column(DateTime, nullable=True)

    notes = Column(Text, nullable=True)
    attention_reason = Column(String, nullable=True)

    created_at = Column(DateTime, default=utcnow)

    posting = relationship("JobPosting", back_populates="application")
    documents = relationship("TailoredDocument", back_populates="application", cascade="all, delete-orphan")
    outreach_messages = relationship("OutreachMessage", back_populates="application", cascade="all, delete-orphan")
    interview_preps = relationship(
        "InterviewPrep", back_populates="application", cascade="all, delete-orphan",
        order_by="InterviewPrep.generated_at.desc()",
    )
    mock_interview_sessions = relationship("MockInterviewSession", back_populates="application", cascade="all, delete-orphan")

    @property
    def active_interview_prep(self):
        return next((p for p in self.interview_preps if p.is_active), None)


class TailoredDocument(Base):
    __tablename__ = "tailored_documents"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False)
    document_type = Column(String, nullable=False)  # 'resume' | 'cover_letter'
    content = Column(Text, nullable=False)
    ats_score = Column(Integer, nullable=True)
    generated_at = Column(DateTime, default=utcnow)

    application = relationship("JobApplication", back_populates="documents")


# ---------------------------------------------------------------------------
# Outreach (review-gated, capped)
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
    created_at = Column(DateTime, default=utcnow)

    application = relationship("JobApplication", back_populates="outreach_messages")


# ---------------------------------------------------------------------------
# Interview prep
# ---------------------------------------------------------------------------

class InterviewPrep(Base):
    """Versioned, same pattern as ProfileVersion -- regenerating used to
    silently overwrite the previous prep in place with no way to compare
    or recover it. Now every generation is a new row; exactly one per
    application has is_active=True at a time (flipped in
    interview_prep_service.generate_interview_prep and
    restore_interview_prep_version), older ones kept for history/restore
    rather than deleted. JobApplication.active_interview_prep is the
    plain-Python-property equivalent of the old uselist=False
    relationship, for callers that only ever want the current one."""
    __tablename__ = "interview_prep"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False)

    general_prep_json = Column(Text, nullable=True)   # questions/talking points based on your background
    company_prep_json = Column(Text, nullable=True)   # company-specific angles, from JD + light research
    process_research_json = Column(Text, nullable=True)  # real reported interview-process findings + sources, when found
    predicted_rounds_json = Column(Text, nullable=True)   # round-by-round structured plan, grounded in process_research when available
    is_active = Column(Boolean, default=True)
    generated_at = Column(DateTime, default=utcnow)

    application = relationship("JobApplication", back_populates="interview_preps")


class BehavioralStory(Base):
    """Reusable STAR-format behavioral story, tied to a profile variant
    rather than a single application -- the same real story gets reused
    across every behavioral/PEI-style round for any job, instead of
    being regenerated from scratch each time. Draft-then-confirm, same
    safeguard posture as tailoring: an LLM-drafted story isn't treated
    as ready-to-use prep material until a human confirms it, and every
    draft must cite which real profile entry it came from."""
    __tablename__ = "behavioral_stories"

    id = Column(Integer, primary_key=True, index=True)
    variant_id = Column(Integer, ForeignKey("profile_variants.id"), nullable=False)

    title = Column(String, nullable=False)
    situation = Column(Text, nullable=False)
    task = Column(Text, nullable=False)
    action = Column(Text, nullable=False)
    result = Column(Text, nullable=False)
    traits_json = Column(Text, nullable=False, default="[]")  # e.g. ["leadership", "ownership"]
    source_reference = Column(String, nullable=True)  # which real experience/project entry this is drawn from
    status = Column(String, nullable=False, default="draft")  # draft | confirmed
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)

    variant = relationship("ProfileVariant")


class MockInterviewSession(Base):
    """One practice run through a single predicted round -- the AI plays
    interviewer, picking an opening question at random from that round's
    already-grounded pool (predicted_rounds' qa_pairs + other_possible_
    questions) and reacting to the candidate's actual answers with
    follow-ups or new questions from the pool, rather than working down
    a fixed list the candidate can see coming. tier controls how forgiving
    the session is (see mock_interview_service.py's TIER_DESCRIPTIONS);
    the adaptive layer can suggest moving up a tier mid-session, but
    never does so without the candidate explicitly accepting."""
    __tablename__ = "mock_interview_sessions"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("job_applications.id"), nullable=False)

    round_name = Column(String, nullable=False)
    tier = Column(String, nullable=False, default="warm_up")  # warm_up | guided | full_simulation
    status = Column(String, nullable=False, default="in_progress")  # in_progress | completed
    debrief_json = Column(Text, nullable=True)  # filled once, at end_session -- accuracy/completeness/structure feedback

    # Camera feedback is opt-in per session (not every round is actually
    # on video, and the candidate is the one who knows their own real
    # scheduled format). visual_metrics_json holds only small aggregated
    # numbers submitted once at end_session (face-forward ratio, a
    # movement count) -- raw video/frames never reach the server, all
    # detection runs client-side. See mock_interview_service.py's
    # docstring for why this deliberately does NOT do facial-expression/
    # emotion inference, only observable, descriptive signals.
    camera_enabled = Column(Boolean, default=False)
    visual_metrics_json = Column(Text, nullable=True)

    started_at = Column(DateTime, default=utcnow)
    ended_at = Column(DateTime, nullable=True)

    application = relationship("JobApplication", back_populates="mock_interview_sessions")
    turns = relationship(
        "MockInterviewTurn", back_populates="session", cascade="all, delete-orphan",
        order_by="MockInterviewTurn.turn_index",
    )


class MockInterviewTurn(Base):
    """One line of the practice conversation. speaker is 'interviewer'
    or 'candidate'; is_followup marks an interviewer turn that reacted
    to the candidate's last answer rather than pulling a fresh question
    from the round's pool -- kept distinct mainly so a transcript view
    can visually show where the conversation branched off-script, same
    as a real interview would."""
    __tablename__ = "mock_interview_turns"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(Integer, ForeignKey("mock_interview_sessions.id"), nullable=False)

    turn_index = Column(Integer, nullable=False)
    speaker = Column(String, nullable=False)  # interviewer | candidate
    content = Column(Text, nullable=False)
    is_followup = Column(Boolean, default=False)
    # Set on an interviewer turn when the adaptive layer judges the
    # candidate is finding the current tier comfortably easy -- surfaced
    # to the candidate, never auto-applied. Persisted (not just returned
    # in-memory from submit_answer) so it's still visible after a page
    # reload, at the point in the transcript it actually happened.
    suggest_level_up = Column(Boolean, default=False)
    level_up_note = Column(Text, nullable=True)
    # Voice-delivery signals, set only on a candidate turn answered by
    # voice (see mock_interview_session.html's SpeechRecognition
    # instrumentation) -- real recorded-speech duration rather than the
    # wall-clock gap between turns (which conflates think-time,
    # speaking-time, and transcript-review-time), plus mid-answer pause
    # tracking from gaps between recognition results while still
    # recording. Null for typed answers, where neither concept applies
    # the same way.
    recording_duration_seconds = Column(Float, nullable=True)
    pause_count = Column(Integer, nullable=True)
    longest_pause_seconds = Column(Float, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    session = relationship("MockInterviewSession", back_populates="turns")


# ---------------------------------------------------------------------------
# Search configuration
# ---------------------------------------------------------------------------

class SearchKeyword(Base):
    __tablename__ = "search_keywords"

    id = Column(Integer, primary_key=True, index=True)
    keyword = Column(String, nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True)


class SeniorityExclusion(Base):
    """Title-level seniority terms to exclude from intake (e.g. "Staff",
    "Director") -- mirrors SearchKeyword's table/UI shape so it gets the
    same live add/toggle/delete management rather than a flat settings
    string. Both this table and SearchKeyword are auto-derived from the
    candidate's own profile the first time intake needs them and finds
    neither configured -- see intake_service.ensure_intake_targeting."""
    __tablename__ = "seniority_exclusions"

    id = Column(Integer, primary_key=True, index=True)
    term = Column(String, nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True)


class LocationExclusion(Base):
    """Location-text terms (country/region names, e.g. "Poland", "India")
    to exclude from intake -- a company's own Greenhouse/Lever/Ashby board
    has no location filter at all, so without this a US-based candidate's
    intake fills up with roles they're not actually eligible for. Mirrors
    SeniorityExclusion's table/UI shape. Seeded with a static default list
    of common non-US location signals the first time intake needs it and
    finds the table empty -- see intake_service.ensure_location_exclusions_
    seeded. Unlike SearchKeyword/SeniorityExclusion, not LLM-derived: "which
    countries aren't the US" doesn't need a profile-grounded guess."""
    __tablename__ = "location_exclusions"

    id = Column(Integer, primary_key=True, index=True)
    term = Column(String, nullable=False, unique=True, index=True)
    is_active = Column(Boolean, default=True)


class JobSource(Base):
    """Which intake sources are enabled, and their own health/quota state
    -- so we can respect each free-tier API budget independently instead
    of one global polling number."""
    __tablename__ = "job_sources"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False, unique=True)  # 'linkedin' | 'adzuna' | 'greenhouse' | 'lever' | 'ashby' | 'recruitee' | 'personio' | 'jobspipe'
    is_active = Column(Boolean, default=True)
    calls_used_this_period = Column(Integer, default=0)
    period_reset_at = Column(DateTime, nullable=True)
    last_polled_at = Column(DateTime, nullable=True)
    last_error = Column(String, nullable=True)
    # For sources that cost one real external call per keyword
    # (Adzuna, LinkedIn) -- where in the active keyword list the next
    # cycle's rotating subset should start, so repeated cycles cover
    # the full list over time instead of re-querying the same few
    # keywords, or every keyword, every cycle.
    keyword_rotation_offset = Column(Integer, default=0)

    # Daily pacing for a hard-capped monthly budget (Adzuna).
    # Without this, calls_used_this_period/period_reset_at alone let a
    # source burn its entire monthly quota in the first day or two at a
    # normal polling cadence, then go completely dark for the rest of
    # the period -- confirmed for real: at this project's own default
    # settings (15-min poll interval, 5 keywords/cycle), Adzuna's real
    # 900-call/month budget was being exhausted in under 2 days.
    calls_used_today = Column(Integer, default=0)
    daily_reset_at = Column(DateTime, nullable=True)


# ---------------------------------------------------------------------------
# Global settings (every tunable number, live-editable)
# ---------------------------------------------------------------------------

class GlobalSettings(Base):
    __tablename__ = "global_settings"

    id = Column(Integer, primary_key=True, index=True)

    # Kill switch
    automation_enabled = Column(Boolean, default=True)

    # Intake cadence
    fast_poll_interval_minutes = Column(Integer, default=10)   # cheap "anything new?" check
    full_ingest_interval_minutes = Column(Integer, default=15)  # full scoring/tailoring pass
    stale_posting_threshold_days = Column(Integer, default=45)  # flag postings open longer than this (warning only)

    # Location targeting -- the search-string param sent to Adzuna/
    # LinkedIn's real search APIs (Greenhouse/Lever/Ashby have no location
    # search param at all; those are filtered locally via LocationExclusion
    # instead, see keyword_matching.location_allowed).
    location_query = Column(String, default="United States")

    # JobRight company-discovery cadence -- the underlying repo
    # only updates once a day, so polling more often than this wastes a
    # fetch for no new data. Not a paid-API-budget concern like Adzuna,
    # but still a real tunable rather than a hardcoded constant.
    jobright_poll_interval_hours = Column(Integer, default=24)

    # Confirmation queue
    confirmation_window_hours = Column(Float, default=15.0)
    fast_track_score_threshold = Column(Integer, default=90)   # very high match...
    fast_track_freshness_minutes = Column(Integer, default=30)  # ...and very fresh -> shrink the window
    fast_track_window_hours = Column(Float, default=2.0)
    rejected_retention_days = Column(Integer, default=7)

    # Minimum match_score required for a clean, autofill-
    # supported application to skip straight to auto-launching a real
    # browser. Originally there was no score gate here at all -- routing
    # only checked for a fabrication/scam/eligibility flag, so even a
    # very low-scoring application could reach a real, unattended
    # browser auto-launch as long as tailoring happened not to trigger
    # the fabrication check. A clean-but-low-scoring application still
    # gets tailored and still gets a normal timed Pending Confirmation
    # window (with notification) below this bar -- it just doesn't skip
    # straight to auto-launch. Default (65) chosen from a natural gap
    # observed between clearly-mismatched postings (28-52%, each with
    # named structural gaps in their gaps_analysis) and plausible
    # near-fits (62-78%). Live-editable; not a judgment this app should
    # hardcode for every user's risk tolerance.
    min_score_for_auto_launch = Column(Integer, default=65)

    # Quiet hours -- a confirmation deadline that would land inside
    # this daily local-time window gets pushed to the end of it, so it
    # never silently lapses while the user is predictably unreachable
    # (e.g. asleep). Generic on purpose -- not specific to one schedule.
    quiet_hours_enabled = Column(Boolean, default=True)
    quiet_hours_start_hour = Column(Integer, default=23)  # local 24h clock
    quiet_hours_end_hour = Column(Integer, default=7)
    local_timezone = Column(String, default="UTC")  # IANA tz name -- set this to your own on the dashboard

    # Notification digest -- individual emails are reserved for
    # fast-track only; everything else batches into one periodic digest
    # so queueing many applications at once can't spam the inbox.
    notification_digest_interval_minutes = Column(Integer, default=30)
    last_digest_sent_at = Column(DateTime, nullable=True)

    # Outreach
    daily_outreach_cap = Column(Integer, default=10)

    # Automated backups -- export used to be manual-only, so a gap
    # unattended for weeks meant zero recent recovery point. Runs once a
    # day (see scheduler.py) straight to local disk (backups/scheduled/,
    # gitignored) using the same encrypted format as the on-demand
    # download, then prunes down to the retention count.
    automated_backups_enabled = Column(Boolean, default=True)
    backup_retention_count = Column(Integer, default=14)

    # Tavily/Hunter.io budget tracking. Unlike Adzuna (polled
    # on a fixed schedule, where daily pacing matters -- see JobSource's
    # calls_used_today), these are called on-demand per human click
    # ("Discover Contact", interview prep's company research), so a
    # simple monthly counter + hard cap fits better than daily pacing.
    # Defaults match each provider's real free-tier limit (see
    # .env.example). Before this, a real quota exhaustion looked
    # identical to "genuinely found nothing" in the logs -- both
    # contact_discovery_service.py functions now distinguish the two.
    tavily_monthly_call_budget = Column(Integer, default=1000)
    tavily_calls_used_this_month = Column(Integer, default=0)
    tavily_month_reset_at = Column(DateTime, nullable=True)
    hunter_monthly_call_budget = Column(Integer, default=25)
    hunter_calls_used_this_month = Column(Integer, default=0)
    hunter_month_reset_at = Column(DateTime, nullable=True)

    # Interview prep no longer caps how many questions surface per round
    # (a real interview isn't bounded by a quota either), but drafting a
    # full, ready-to-say answer for every single one is what actually
    # drives real LLM cost/latency -- confirmed the hard way (3 failed
    # generations from JSON truncation before this existed). This caps
    # how many get a FULL drafted answer per round; anything beyond that
    # still surfaces as a plain question (no answer) instead of vanishing.
    # Defaults low/free-tier-friendly since this is a public platform
    # forkers may run on a free-tier key, not just this deployment --
    # live-editable per instance for anyone (like the operator here) who
    # wants deeper prep and is fine paying more for it.
    interview_prep_answer_target = Column(Integer, default=8)

    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


def get_or_create_settings(db) -> "GlobalSettings":
    """A brand-new deployment in showcase mode gets automation OFF by
    default -- applied only at row-creation time, not as a forced
    override on every read, so it's a real, user-toggleable default (a
    showcase forker can still explicitly opt in after reading the
    ethical-use docs) rather than a hard lock. Existing deployments
    already have a row, so this has zero effect on them regardless of
    APP_MODE."""
    settings = db.query(GlobalSettings).first()
    if not settings:
        settings = GlobalSettings(automation_enabled=not is_showcase_mode())
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
    timestamp = Column(DateTime, default=utcnow)
