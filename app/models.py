"""
Data model for the job-search CRM/automation platform.

Designed against the full roadmap up front so later phases (confirmation
queue, outreach, interview prep, analytics) don't require bolt-on schema
surgery. Not every column is used by the code that exists yet -- that's
intentional; it means each phase just fills in behavior against a schema
that already fits, instead of migrating tables mid-project.
"""

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, ForeignKey, Boolean, Float, JSON,
    UniqueConstraint, CheckConstraint,
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
    workable_slug = Column(String, nullable=True)
    smartrecruiters_slug = Column(String, nullable=True)  # SmartRecruiters' own "company identifier", not a URL slug
    board_slugs_checked_at = Column(DateTime, nullable=True)

    # H-1B sponsorship signal, sourced from the USCIS H-1B Employer Data
    # Hub CSV (see ingest/sponsors.py). Null on every one of these columns
    # means "not yet matched against USCIS data," not "confirmed zero
    # activity" -- h1b_data_updated_at is what distinguishes the two.
    h1b_approvals_total = Column(Integer, nullable=True)  # summed across all FYs present in the loaded CSV
    h1b_denials_total = Column(Integer, nullable=True)
    h1b_last_fiscal_year = Column(Integer, nullable=True)  # most recent FY with any recorded activity
    # 'Frequent' | 'Occasional' | 'Rare' | 'None' -- bucketed from
    # h1b_approvals_total at ingest time so scoring (later phase) doesn't
    # re-derive the same thresholds in multiple places.
    sponsorship_tier = Column(String, nullable=True)
    h1b_data_updated_at = Column(DateTime, nullable=True)  # last time ingest/sponsors.py touched this row

    # LCA filings are a second, more current sponsorship signal -- an
    # employer files a Labor Condition Application with DOL *before* the
    # H-1B petition itself, so a recent LCA filing can show real intent
    # to sponsor even for an employer whose USCIS approval history above
    # is thin or stale. Sourced from the DOL OFLC LCA Disclosure quarterly
    # XLSX (see ingest/sponsors.py).
    lca_filings_total = Column(Integer, nullable=True)
    lca_last_fiscal_quarter = Column(String, nullable=True)  # e.g. "FY2024Q1"
    lca_data_updated_at = Column(DateTime, nullable=True)

    # Cap-exempt employers (universities, nonprofit/gov research orgs,
    # affiliated nonprofits) aren't subject to the annual H-1B lottery --
    # a real, positive signal distinct from "no USCIS filing history yet"
    # for a company that may not need to win the cap to sponsor. Matched
    # against ingest/cap_exempt_seeds.yaml, not USCIS data (USCIS doesn't
    # label this directly).
    is_cap_exempt = Column(Boolean, default=False)
    cap_exempt_reason = Column(String, nullable=True)  # which seed entry/category matched

    # Highest DOL prevailing-wage level ("I"-"IV") this company has ever
    # filed a certified H-1B LCA at for a Computer/Mathematical role (SOC
    # 15-* -- see ingest/sponsors.py's load_dol_lca_data). A proxy for
    # "does this company pay/level tech roles well," distinct from
    # wage_level_for()'s per-posting offered-salary comparison (Phase 1)
    # -- this app has no per-posting offered-salary field to compare
    # against, so the score component described in WEIGHTING.md uses this
    # company-level signal instead, exactly as specified.
    max_wage_level_15xx = Column(String, nullable=True)

    # A/B/C/X, derived from h1b_approvals_total/lca_filings_total/
    # is_cap_exempt/max_wage_level_15xx via company_tier.derive_tier() --
    # never hand-set, always recomputed by the backfill CLI or the
    # scoring path when stale. See WEIGHTING.md for the exact thresholds
    # (config-driven via GlobalSettings, not hardcoded here).
    tier = Column(String, nullable=True)
    tier_computed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("tier IN ('A', 'B', 'C', 'X')", name="ck_companies_tier"),
        CheckConstraint("max_wage_level_15xx IN ('I', 'II', 'III', 'IV')", name="ck_companies_max_wage_level"),
    )

    postings = relationship("JobPosting", back_populates="company")


# ---------------------------------------------------------------------------
# H-1B wage benchmarking (DOL OFLC OEWS data)
# ---------------------------------------------------------------------------

class OewsWage(Base):
    """One SOC occupation code's prevailing-wage levels for one OEWS
    geographic area, loaded from the DOL OFLC OEWS wage data file (see
    ingest/wages.py). Levels I-IV follow the OFLC prevailing-wage
    convention (roughly the 17th/34th/50th/67th wage percentiles for the
    occupation+area) -- used by wage_level_for() to classify an offered
    salary against what DOL would expect for that role/location."""

    __tablename__ = "oews_wages"

    id = Column(Integer, primary_key=True, index=True)
    soc_code = Column(String, nullable=False, index=True)  # e.g. "15-1252" (Software Developers)
    soc_title = Column(String, nullable=True)
    area_title = Column(String, nullable=False, index=True)  # OEWS area name, e.g. "San Jose-Sunnyvale-Santa Clara, CA"
    source_year = Column(Integer, nullable=False)  # OEWS release year the wage data is from

    wage_level_1 = Column(Float, nullable=True)  # annual, USD
    wage_level_2 = Column(Float, nullable=True)
    wage_level_3 = Column(Float, nullable=True)
    wage_level_4 = Column(Float, nullable=True)

    updated_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        UniqueConstraint("soc_code", "area_title", "source_year", name="uq_oews_wage_soc_area_year"),
    )


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

    # Pure regex sponsorship-signal extraction from the JD text, run
    # in-process at intake -- no LLM, no cost (see
    # sponsorship_signals.py). Distinct from JobApplication.
    # visa_sponsorship, an LLM-derived classification computed later
    # and only when an application is actually scored (a real cost);
    # this is the free, always-on signal the hard gate uses. blocked
    # wins over signal at the GATING layer (queue_service.py), not by
    # zeroing sponsorship_signal here -- both booleans reflect their
    # own independent pattern matches, signal_matches records exactly
    # which patterns fired in each category for audit.
    sponsorship_blocked = Column(Boolean, default=False)
    sponsorship_signal = Column(Boolean, default=False)
    worksite_ambiguous = Column(Boolean, default=False)
    signal_matches = Column(JSON, nullable=True)

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
    replied_at = Column(DateTime, nullable=True)  # self-reported, same trust model as interviewing_at/offer_at below
    interviewing_at = Column(DateTime, nullable=True)  # only set by an explicit Mark as Interviewing click
    offer_at = Column(DateTime, nullable=True)
    not_selected_at = Column(DateTime, nullable=True)

    notes = Column(Text, nullable=True)
    attention_reason = Column(String, nullable=True)

    # Mechanical score components (sponsorship/wage/worksite/AI-profile-fit),
    # one entry per component with its raw value/weight/contribution -- see
    # scoring_service.py and WEIGHTING.md. Null until score_application()
    # (or the backfill CLI) has run at least once for this application.
    score_breakdown = Column(JSON, nullable=True)

    # /queue triage -- Skip (soft, revisable, excluded from the default
    # queue view but never deleted) is distinct from status="Rejected"
    # (existing hard decline, swept after rejected_retention_days): both
    # reuse this same short reason enum (see queue_service.py), skipped_at
    # is what distinguishes a genuine Skip from a Rejected/"Not a fit"
    # application, which sets status instead and leaves skipped_at unset.
    skip_reason = Column(String, nullable=True)
    skipped_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "skip_reason IN ('not_interested', 'low_priority', 'duplicate_role', 'bad_timing', 'other')",
            name="ck_job_applications_skip_reason",
        ),
    )

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

    # Outreach hygiene caps (per-person lifetime, per-company rolling
    # window -- see outreach_hygiene.py) block drafting by default when
    # violated; cap_override_reason is set only when the user explicitly
    # typed a reason to override the block, and is left null on every
    # normal, non-overridden draft.
    cap_override_reason = Column(String, nullable=True)

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
    name = Column(String, nullable=False, unique=True)  # 'linkedin' | 'adzuna' | 'greenhouse' | 'lever' | 'ashby' | 'recruitee' | 'personio' | 'workable' | 'smartrecruiters' | 'remoteok' | 'remotive' | 'weworkremotely' | 'jobspresso' | 'jobspipe' | 'jobright' | 'ats_dataset' | 'job_board_aggregator' | 'yc_directory'
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

    # Bulk company-discovery datasets (ats_dataset_discovery.py,
    # job_board_aggregator_discovery.py) -- free, open, daily-refreshed
    # third-party datasets mapping real companies to real ATS slugs.
    # Polled on the same slow cadence as jobright (re-fetching a
    # multi-thousand-row dataset doesn't need to happen often); each
    # cycle only ingests a capped batch of new companies
    # (bulk_discovery_batch_size) so verification-probe traffic against
    # Greenhouse/Lever/etc doesn't spike and the Company table grows
    # gradually across cycles instead of all at once.
    bulk_discovery_poll_interval_hours = Column(Integer, default=24)
    bulk_discovery_batch_size = Column(Integer, default=25)

    # Phase 2b remote-board sources (RemoteOK/Remotive/WeWorkRemotely/
    # Jobspresso) -- one shared slow cadence across all 4, sized to
    # Remotive's own stricter published API constraint ("advise max. 4
    # times a day" -- see remotive_source.py), not each source's own
    # looser advisory (e.g. WeWorkRemotely's RSS <ttl> of 60 minutes).
    remote_board_poll_interval_minutes = Column(Integer, default=360)

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

    # Company.tier (A/B/C/X) thresholds -- company_tier.py reads these
    # instead of hardcoding them, so the bar for "proven sponsor" is a
    # live-editable judgment call, not baked into code. tier_a_min_wage_level
    # is stored as an int 1-4 (I-IV) for a plain >= comparison against
    # Company.max_wage_level_15xx (converted via company_tier.WAGE_LEVEL_RANK).
    tier_a_min_filings = Column(Integer, default=10)
    tier_a_min_wage_level = Column(Integer, default=3)
    tier_b_min_filings = Column(Integer, default=3)

    # Outreach hygiene caps (outreach_hygiene.py) -- these guard against
    # re-contacting the same person/company too often, checked at draft
    # time, not send time (no daily send-volume ceiling exists anywhere
    # in this app, deliberately). This app never sets, displays, or
    # implies a target number of applications/messages per day or week
    # -- it counts what happened, it doesn't tell the user what they
    # should do (see queue_service.py/metrics_service.py -- no daily/
    # weekly target fields exist anywhere in this schema, deliberately).
    outreach_per_person_lifetime = Column(Integer, default=1)  # cold messages to the same person, ever
    outreach_per_company_max = Column(Integer, default=10)  # messages to the same company...
    outreach_per_company_window_days = Column(Integer, default=14)  # ...within this many rolling days

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


# ---------------------------------------------------------------------------
# Adaptive layer (Part B) -- audit trail for every self-tuning change
# ---------------------------------------------------------------------------

class AdaptationLog(Base):
    """One row per adaptive change, either tier -- see
    app/services/adaptation_service.py. Every field the guardrails
    require is here on purpose: what changed, the old/new value, the
    evidence it was based on, the sample size, and whether it was
    auto-applied (Tier 1) or required human approval (Tier 2). Nothing
    in this table is ever deleted -- a revert writes a NEW row
    restoring the old value and marks the original 'reverted' via
    reverted_at, so the audit trail itself stays append-only."""

    __tablename__ = "adaptation_log"

    id = Column(Integer, primary_key=True, index=True)
    tier = Column(String, nullable=False)  # 'tier1' | 'tier2'
    subsystem = Column(String, nullable=False, index=True)  # e.g. 'dedupe_threshold', 'resume_variant_reply_rate'
    parameter = Column(String, nullable=False)  # which specific config key/value changed
    old_value = Column(JSON, nullable=True)
    new_value = Column(JSON, nullable=True)
    triggering_evidence = Column(JSON, nullable=True)  # sample sizes, computed rates/CIs -- whatever justified this
    sample_size = Column(Integer, nullable=True)

    # Tier 1 rows are created with status="applied" directly (auto-
    # applies, no approval gate). Tier 2 rows start "proposed" and only
    # ever become "approved" or "rejected" through an explicit human
    # click (see /adaptation) -- never silently transition themselves.
    status = Column(String, default="applied")

    created_at = Column(DateTime, default=utcnow)
    reverted_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint("tier IN ('tier1', 'tier2')", name="ck_adaptation_log_tier"),
        CheckConstraint(
            "status IN ('applied', 'proposed', 'approved', 'rejected', 'reverted')",
            name="ck_adaptation_log_status",
        ),
    )


class AdaptiveParameterValue(Base):
    """The CURRENT effective value of one self-tuned parameter --
    config/adaptation.yaml declares each parameter's default and
    allowed [min, max] bounds (static, human-edited); this table holds
    what it's actually drifted to since (dynamic, app-written). A
    parameter with no row here is at its config default -- cold start
    (b3.4) falls out of that naturally, no special-case code needed.
    Every write here is paired with an AdaptationLog row explaining why."""

    __tablename__ = "adaptive_parameter_values"

    id = Column(Integer, primary_key=True, index=True)
    parameter = Column(String, nullable=False, unique=True)
    value = Column(Float, nullable=False)
    updated_at = Column(DateTime, default=utcnow)
