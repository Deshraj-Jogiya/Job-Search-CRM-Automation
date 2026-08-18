"""
Phase 2: multi-source job intake. Polls each configured source at its
own cadence (LinkedIn is free to hit often; Adzuna's free tier has a
real monthly call budget, so it's polled less often and hard-capped),
dedupes against existing postings (exact by source+external_id/url,
fuzzy by normalized company+title), flags scam-pattern JDs and stale
listings as warnings (never filters them out -- see CLAUDE.md), and
creates JobPosting + JobApplication rows for anything genuinely new.

Every background job here checks GlobalSettings.automation_enabled
fresh before doing real work, per CLAUDE.md's kill-switch convention.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Company, JobApplication, JobPosting, JobSource, SearchKeyword, get_or_create_settings
from . import board_discovery
from .activity_logger import log_activity
from .company_utils import normalize_company_name, normalize_title
from .sources import adzuna_source, ashby_source, greenhouse_source, lever_source, linkedin_source

SOURCE_MODULES = {
    linkedin_source.SOURCE_NAME: linkedin_source,
    adzuna_source.SOURCE_NAME: adzuna_source,
    greenhouse_source.SOURCE_NAME: greenhouse_source,
    lever_source.SOURCE_NAME: lever_source,
    ashby_source.SOURCE_NAME: ashby_source,
}

# How many pre-existing companies (created before board-slug auto-
# detection existed, or never probed for some other reason) get probed
# per intake cycle. Caps the number of outbound probe requests per tick
# instead of hammering every unchecked company's board APIs at once.
_BOARD_SLUG_BACKFILL_BATCH = 10

# How long a gap in last_seen_at before a company+title match is treated
# as a genuine repost (new JobPosting row) rather than the same listing
# still being live (just bump last_seen_at).
_REPOST_GAP_DAYS = 14

_DEFAULT_KEYWORDS = [
    "Machine Learning Engineer",
    "Data Engineer",
    "Data Scientist",
    "Applied Machine Learning Scientist",
    "Data Analyst",
]
_DEFAULT_LOCATION = "United States"

_ADZUNA_MONTHLY_CALL_BUDGET = int(os.getenv("ADZUNA_MONTHLY_CALL_BUDGET", "900"))

_SCAM_PATTERNS = [
    (r"\bwire transfer\b", "mentions wire transfer"),
    (r"\bprocessing fee\b", "mentions a processing fee"),
    (r"\bpurchase (a |your own )?(laptop|equipment|starter kit)\b", "asks candidate to buy equipment"),
    (r"\bsend (us |your )?(money|payment|deposit)\b", "asks for payment"),
    (r"\b(whatsapp|telegram) only\b", "off-platform-only contact (WhatsApp/Telegram)"),
    (r"\bno interview (necessary|required|needed)\b", "claims no interview is needed"),
]


def _get_active_keywords(db: Session) -> list[str]:
    active = db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()  # noqa: E712
    return [k.keyword for k in active] if active else _DEFAULT_KEYWORDS


def _get_or_create_job_source(db: Session, name: str) -> JobSource:
    source = db.query(JobSource).filter(JobSource.name == name).first()
    if not source:
        source = JobSource(name=name, is_active=True)
        db.add(source)
        db.commit()
        db.refresh(source)
    return source


def _is_due(source: JobSource, interval_minutes: int, now: datetime) -> bool:
    if source.last_polled_at is None:
        return True
    return (now - source.last_polled_at) >= timedelta(minutes=interval_minutes)


def _reset_call_period_if_needed(source: JobSource, now: datetime) -> None:
    if source.period_reset_at is None or now >= source.period_reset_at:
        source.calls_used_this_period = 0
        source.period_reset_at = now + timedelta(days=30)


def _detect_scam_patterns(jd_text: str) -> str | None:
    if not jd_text:
        return None
    lower = jd_text.lower()
    hits = [reason for pattern, reason in _SCAM_PATTERNS if re.search(pattern, lower)]
    return "; ".join(hits) if hits else None


def _apply_discovered_slugs(db: Session, company: Company, slugs: dict) -> None:
    company.greenhouse_slug = slugs.get("greenhouse")
    company.lever_slug = slugs.get("lever")
    company.ashby_slug = slugs.get("ashby")
    company.board_slugs_checked_at = datetime.utcnow()
    found = [ats for ats, slug in slugs.items() if slug]
    if found:
        log_activity(db, f"Detected {', '.join(found)} board(s) for {company.name}.", "INFO")


def _get_or_create_company(db: Session, raw_name: str) -> Company:
    """Board-slug probing does NOT happen inline here -- a single
    ingestion pass can create many new companies at once (e.g. a big
    first LinkedIn scan), and each probe is several real network calls.
    Probing every new company inline would make ingestion latency scale
    with how many new companies showed up this cycle, which defeats the
    whole point of frequent polling. New companies are left unchecked
    (board_slugs_checked_at=None) and picked up by the capped
    _backfill_board_slugs() sweep instead, same as any other unchecked
    company -- bounded cost per cycle regardless of ingest volume."""
    normalized = normalize_company_name(raw_name)
    company = db.query(Company).filter(Company.normalized_name == normalized).first()
    if not company:
        company = Company(name=raw_name, normalized_name=normalized)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


def set_manual_board_slug(db: Session, company_name: str, ats_type: str, slug: str) -> Company:
    """User-asserted override/addition from the Jobs page -- for
    companies auto-detection missed (non-obvious slug) or hasn't run for
    yet. Deliberately synchronous and network-free (the user is
    supplying the slug directly, nothing to probe) -- marks
    board_slugs_checked_at so the backfill sweep leaves this company
    alone afterward, since a fresh discover_slugs() call would overwrite
    all three slug fields wholesale and could clobber exactly the
    override the user just made (the auto-probe already missed this
    slug once, or it wouldn't have needed a manual entry). This does
    mean the other two ATS types won't get auto-probed for this company
    -- an acceptable gap; the user can set those manually too if needed."""
    if ats_type not in ("greenhouse", "lever", "ashby"):
        raise ValueError(f"Unknown ATS type '{ats_type}'.")
    if not slug or not slug.strip():
        raise ValueError("Slug can't be empty.")

    normalized = normalize_company_name(company_name)
    company = db.query(Company).filter(Company.normalized_name == normalized).first()
    if not company:
        company = Company(name=company_name, normalized_name=normalized)
        db.add(company)
        db.commit()
        db.refresh(company)

    setattr(company, f"{ats_type}_slug", slug.strip())
    company.board_slugs_checked_at = datetime.utcnow()
    db.commit()
    log_activity(db, f"Manually set {ats_type} slug for {company.name}: {slug.strip()}", "INFO")
    return company


def _backfill_board_slugs(db: Session) -> None:
    """Probes a capped batch of not-yet-checked companies each cycle --
    this is the ONLY place board slugs get probed (new companies from
    this cycle's ingestion included; see _get_or_create_company's
    docstring for why probing isn't inline there). The network fetch
    (discover_slugs, several requests per company) runs concurrently
    across the batch -- SQLAlchemy Sessions aren't thread-safe, so the
    actual DB writes happen back on this thread, sequentially, once all
    fetches return."""
    unchecked = (
        db.query(Company)
        .filter(Company.board_slugs_checked_at.is_(None))
        .limit(_BOARD_SLUG_BACKFILL_BATCH)
        .all()
    )
    if not unchecked:
        return

    with ThreadPoolExecutor(max_workers=min(len(unchecked), 5)) as pool:
        results = list(pool.map(lambda c: board_discovery.discover_slugs(c.name), unchecked))

    for company, slugs in zip(unchecked, results):
        _apply_discovered_slugs(db, company, slugs)
    db.commit()


def _find_matching_posting(db: Session, company_id: int, raw) -> tuple[JobPosting | None, bool]:
    """Returns (matched_posting, is_repost). matched_posting is None if
    nothing matches at all."""
    if raw.external_id:
        exact = (
            db.query(JobPosting)
            .filter(JobPosting.source == raw.source, JobPosting.external_id == raw.external_id)
            .first()
        )
        if exact:
            return exact, False

    exact_url = db.query(JobPosting).filter(JobPosting.job_url == raw.job_url).first()
    if exact_url:
        return exact_url, False

    normalized_title = normalize_title(raw.job_title)
    candidates = db.query(JobPosting).filter(JobPosting.company_id == company_id).all()
    for candidate in candidates:
        if normalize_title(candidate.job_title) == normalized_title:
            gap = datetime.utcnow() - candidate.last_seen_at
            return candidate, gap > timedelta(days=_REPOST_GAP_DAYS)

    return None, False


def _ingest_raw_posting(db: Session, module, raw) -> JobPosting | None:
    company = _get_or_create_company(db, raw.company_name_raw)
    matched, is_repost = _find_matching_posting(db, company.id, raw)

    if matched and not is_repost:
        matched.last_seen_at = datetime.utcnow()
        db.commit()
        return None

    job_description = raw.job_description or module.fetch_full_description(raw)
    if not job_description or len(job_description) < 50:
        return None

    posting = JobPosting(
        company_id=company.id,
        company_name_raw=raw.company_name_raw,
        job_title=raw.job_title,
        job_url=raw.job_url,
        job_description=job_description,
        source=raw.source,
        external_id=raw.external_id,
        first_seen_at=raw.posted_at or datetime.utcnow(),
        last_seen_at=datetime.utcnow(),
        repost_count=(matched.repost_count + 1) if is_repost else 0,
        scam_flag_reason=_detect_scam_patterns(job_description),
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)

    db.add(JobApplication(posting_id=posting.id, status="Ingested"))
    db.commit()

    if is_repost:
        log_activity(
            db,
            f"Detected repost: '{raw.job_title}' at {raw.company_name_raw} "
            f"(seen {posting.repost_count}x, last seen {matched.last_seen_at.strftime('%Y-%m-%d')}).",
            "INFO",
        )
    if posting.scam_flag_reason:
        log_activity(
            db,
            f"Scam-pattern warning on '{raw.job_title}' at {raw.company_name_raw}: {posting.scam_flag_reason}",
            "WARNING",
        )

    return posting


def _run_source(db: Session, module, source_row: JobSource) -> None:
    now = datetime.utcnow()

    if not module.is_configured():
        if source_row.last_error != "not_configured":
            log_activity(db, f"Skipping {module.SOURCE_NAME}: not configured (missing credentials).", "WARNING")
        source_row.last_error = "not_configured"
        source_row.last_polled_at = now
        db.commit()
        return

    _reset_call_period_if_needed(source_row, now)
    if module.SOURCE_NAME == adzuna_source.SOURCE_NAME and source_row.calls_used_this_period >= _ADZUNA_MONTHLY_CALL_BUDGET:
        log_activity(db, f"Skipping {module.SOURCE_NAME}: monthly call budget reached.", "WARNING")
        db.commit()
        return

    keywords = _get_active_keywords(db)
    try:
        raw_postings = module.cheap_scan(keywords, _DEFAULT_LOCATION, limit=15)
    except Exception as e:
        source_row.last_error = str(e)[:250]
        source_row.last_polled_at = now
        db.commit()
        log_activity(db, f"{module.SOURCE_NAME} intake failed: {e}", "ERROR")
        return

    source_row.calls_used_this_period += len(keywords)
    source_row.last_polled_at = now
    source_row.last_error = None
    db.commit()

    log_activity(
        db, f"{module.SOURCE_NAME}: scanned {len(raw_postings)} listing(s) across {len(keywords)} keyword(s).", "INFO"
    )

    ingested = 0
    for raw in raw_postings:
        if _ingest_raw_posting(db, module, raw):
            ingested += 1

    log_activity(db, f"{module.SOURCE_NAME}: ingested {ingested} new posting(s).", "INFO")


def _flag_stale_postings(db: Session, threshold_days: int) -> None:
    cutoff = datetime.utcnow() - timedelta(days=threshold_days)
    stale = (
        db.query(JobPosting)
        .filter(JobPosting.first_seen_at < cutoff, JobPosting.staleness_flag == False)  # noqa: E712
        .all()
    )
    for posting in stale:
        posting.staleness_flag = True
    if stale:
        db.commit()
        log_activity(db, f"Flagged {len(stale)} posting(s) as stale (open > {threshold_days} days).", "INFO")


def run_intake_cycle(db: Session, force: bool = False) -> None:
    """Entry point called by the scheduler, and by the manual 'run now'
    trigger with force=True. Polls whichever sources are due, respecting
    the kill switch, each source's own cadence, and Adzuna's call
    budget. force=True bypasses the cadence check (so a manual click
    actually does something instead of silently no-op'ing if a source
    was already polled recently) but still respects Adzuna's hard
    monthly budget cap -- that's a real quota limit, not just politeness."""
    settings = get_or_create_settings(db)
    if not settings.automation_enabled:
        return

    now = datetime.utcnow()

    linkedin_row = _get_or_create_job_source(db, linkedin_source.SOURCE_NAME)
    if linkedin_row.is_active and (force or _is_due(linkedin_row, settings.fast_poll_interval_minutes, now)):
        _run_source(db, linkedin_source, linkedin_row)

    adzuna_row = _get_or_create_job_source(db, adzuna_source.SOURCE_NAME)
    if adzuna_row.is_active and (force or _is_due(adzuna_row, settings.full_ingest_interval_minutes, now)):
        _run_source(db, adzuna_source, adzuna_row)

    # Direct-ATS sources (Phase 2 slice 2): no search quota to respect,
    # same low-indexing-lag rationale as LinkedIn -- poll at the fast
    # cadence. Each is a no-op (skipped with a clear log entry) until at
    # least one Company row has that ATS's slug set.
    for module in (greenhouse_source, lever_source, ashby_source):
        source_row = _get_or_create_job_source(db, module.SOURCE_NAME)
        if source_row.is_active and (force or _is_due(source_row, settings.fast_poll_interval_minutes, now)):
            _run_source(db, module, source_row)

    _backfill_board_slugs(db)
    _flag_stale_postings(db, settings.stale_posting_threshold_days)
