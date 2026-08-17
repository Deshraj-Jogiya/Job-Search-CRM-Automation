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
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..models import Company, JobApplication, JobPosting, JobSource, SearchKeyword, get_or_create_settings
from .activity_logger import log_activity
from .company_utils import normalize_company_name, normalize_title
from .sources import adzuna_source, linkedin_source

SOURCE_MODULES = {
    linkedin_source.SOURCE_NAME: linkedin_source,
    adzuna_source.SOURCE_NAME: adzuna_source,
}

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


def _get_or_create_company(db: Session, raw_name: str) -> Company:
    normalized = normalize_company_name(raw_name)
    company = db.query(Company).filter(Company.normalized_name == normalized).first()
    if not company:
        company = Company(name=raw_name, normalized_name=normalized)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


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

    _flag_stale_postings(db, settings.stale_posting_threshold_days)
