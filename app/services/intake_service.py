"""
Multi-source job intake. Polls each configured source at its own
cadence (LinkedIn is free to hit often; Adzuna's free tier has a real
monthly call budget, so it's polled less often and hard-capped),
dedupes against existing postings (exact by source+external_id/url,
fuzzy by normalized company+title), flags scam-pattern JDs and stale
listings as warnings (never filters them out), and creates JobPosting
+ JobApplication rows for anything genuinely new.

Every background job here checks GlobalSettings.automation_enabled
fresh before doing real work, so a mid-run toggle takes effect
immediately rather than waiting for the current pass to finish.
"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from ..database import utcnow
from ..models import (
    Company,
    GlobalSettings,
    JobApplication,
    JobPosting,
    JobSource,
    LocationExclusion,
    SearchKeyword,
    SeniorityExclusion,
    get_or_create_settings,
)
from . import ats_dataset_discovery, board_discovery, job_board_aggregator_discovery, jobright_discovery, yc_directory_discovery
from .activity_logger import log_activity
from .company_utils import normalize_company_name, normalize_title
from .llm import get_llm_provider, parse_json_response
from .profile_service import get_default_profile_content
from .sources import (
    adzuna_source,
    ashby_source,
    greenhouse_source,
    jobspipe_source,
    lever_source,
    linkedin_source,
    personio_source,
    recruitee_source,
)

SOURCE_MODULES = {
    linkedin_source.SOURCE_NAME: linkedin_source,
    adzuna_source.SOURCE_NAME: adzuna_source,
    greenhouse_source.SOURCE_NAME: greenhouse_source,
    lever_source.SOURCE_NAME: lever_source,
    ashby_source.SOURCE_NAME: ashby_source,
    recruitee_source.SOURCE_NAME: recruitee_source,
    personio_source.SOURCE_NAME: personio_source,
    jobspipe_source.SOURCE_NAME: jobspipe_source,
}

# Sources whose cheap_scan() makes one real external call per keyword
# (a real search API), as opposed to Greenhouse/Lever/Ashby's one call
# per company regardless of keyword count -- see _run_source. JobsPipe
# isn't in this set: its search API accepts every keyword as a single
# `job_title_or` filter array in one call, so it gets the full keyword
# list every cycle like the direct-ATS sources do.
_PER_KEYWORD_CALL_SOURCES = {adzuna_source.SOURCE_NAME, linkedin_source.SOURCE_NAME}

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

# Static default location-exclusion seed -- not LLM-derived (unlike
# SearchKeyword/SeniorityExclusion), since "which countries aren't the
# US" doesn't need a profile-grounded guess. Common non-US location
# signals seen on Greenhouse/Lever/Ashby boards for companies that also
# hire in the US; editable/extendable from the Jobs page like any other
# exclusion list.
_DEFAULT_LOCATION_EXCLUSIONS = [
    "United Kingdom", "Canada", "India", "Germany", "France", "Ireland",
    "Netherlands", "Spain", "Portugal", "Philippines", "Mexico", "Brazil",
    "Australia", "Singapore", "Ukraine", "Romania", "Argentina", "Colombia",
    "Japan", "China", "Vietnam", "Pakistan", "Nigeria", "South Africa",
    "Israel", "Italy", "Sweden", "Switzerland", "Austria", "Belgium",
    "Denmark", "Finland", "Norway", "New Zealand", "Poland", "EMEA",
    "APAC", "LATAM",
]

_ADZUNA_MONTHLY_CALL_BUDGET = int(os.getenv("ADZUNA_MONTHLY_CALL_BUDGET", "900"))

# JobsPipe bills per job actually returned ("1 credit = 1 job"), not per
# call like Adzuna -- see _quota_cost. Free tier is 1,000 jobs/month;
# kept the same safety-margin convention as Adzuna's own budget default.
_JOBSPIPE_MONTHLY_JOB_BUDGET = int(os.getenv("JOBSPIPE_MONTHLY_JOB_BUDGET", "900"))

# Sources with a real external quota to pace against, and their monthly
# budget. Both share the same reset/daily-pacing machinery
# (_reset_call_period_if_needed, _adzuna_daily_budget) despite the name
# -- it was written for Adzuna first but takes the budget as a plain
# parameter, so it's generic.
_QUOTA_BUDGETED_SOURCES = {
    adzuna_source.SOURCE_NAME: _ADZUNA_MONTHLY_CALL_BUDGET,
    jobspipe_source.SOURCE_NAME: _JOBSPIPE_MONTHLY_JOB_BUDGET,
}

# Hard cap on auto-derived keywords, independent of what the LLM
# actually proposes -- see ensure_intake_targeting.
_MAX_AUTO_KEYWORDS = 20

# Adzuna and LinkedIn each cost one real external call per keyword, per
# cycle -- rotating through a bounded subset instead of using every
# active keyword every cycle keeps per-cycle cost bounded regardless of
# how many keywords are configured, while still covering the full list
# over successive cycles. Greenhouse/Lever/Ashby fetch a company's
# whole board in one call and filter titles locally, so they're exempt.
_KEYWORDS_PER_CYCLE = 5

_SCAM_PATTERNS = [
    (r"\bwire transfer\b", "mentions wire transfer"),
    (r"\bprocessing fee\b", "mentions a processing fee"),
    (r"\bpurchase (a |your own )?(laptop|equipment|starter kit)\b", "asks candidate to buy equipment"),
    (r"\bsend (us |your )?(money|payment|deposit)\b", "asks for payment"),
    (r"\b(whatsapp|telegram) only\b", "off-platform-only contact (WhatsApp/Telegram)"),
    (r"\bno interview (necessary|required|needed)\b", "claims no interview is needed"),
]

# Hard eligibility/compliance requirements stated explicitly in the JD --
# deliberately narrow and limited to unambiguous compliance/authorization
# facts (citizenship, clearance, regulated-data handling), not general
# domain-skill mismatches (those are already well-covered by the LLM match
# score's own "Gaps" analysis). Whether a given requirement actually
# excludes this candidate depends on personal facts this project doesn't
# assume -- this only decides whether to flag, never whether to filter;
# see JobPosting.eligibility_flag_reason.
_ELIGIBILITY_PATTERNS = [
    (r"\bu\.?s\.?\s*citizen(ship)?\b", "requires U.S. citizenship"),
    (r"\b(active|current|eligible to (obtain|hold))[\w\s]{0,20}security clearance\b", "requires a security clearance"),
    (r"\btop secret\b", "requires Top Secret clearance"),
    (r"\bitar\b", "ITAR-restricted (U.S. persons only)"),
    (r"\bhipaa\b", "handles HIPAA-regulated healthcare data"),
    (r"\bprotected health information\b|\bphi\b", "handles protected health information (PHI)"),
]


def _detect_eligibility_flags(jd_text: str) -> str | None:
    if not jd_text:
        return None
    lower = jd_text.lower()
    hits = [reason for pattern, reason in _ELIGIBILITY_PATTERNS if re.search(pattern, lower)]
    return "; ".join(hits) if hits else None


def _get_active_keywords(db: Session) -> list[str]:
    active = db.query(SearchKeyword).filter(SearchKeyword.is_active == True).all()  # noqa: E712
    return [k.keyword for k in active] if active else _DEFAULT_KEYWORDS


def _rotating_keyword_subset(keywords: list[str], offset: int, count: int) -> tuple[list[str], int]:
    """Returns (this cycle's subset, the next cycle's offset). Wraps
    around the list so every keyword eventually gets covered rather
    than only ever searching the first `count` of them."""
    if not keywords:
        return [], 0
    if len(keywords) <= count:
        return keywords, 0
    offset = offset % len(keywords)
    subset = [keywords[(offset + i) % len(keywords)] for i in range(count)]
    return subset, (offset + count) % len(keywords)


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


def _reset_daily_counter_if_needed(source: JobSource, now: datetime) -> None:
    if source.daily_reset_at is None or now >= source.daily_reset_at:
        source.calls_used_today = 0
        source.daily_reset_at = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)


def _adzuna_daily_budget(source: JobSource, now: datetime, monthly_budget: int) -> int:
    """Spreads whatever's left of the monthly budget evenly across the
    days remaining in the current period, instead of letting a normal
    polling cadence burn the whole thing in the first day or two and
    then go dark for the rest of the month (see JobSource.calls_used_
    today's docstring for the confirmed real numbers). Recomputed fresh
    each call from the CURRENT remaining budget/days -- if some days
    used less than their share, later days automatically get a bit
    more, rather than a fixed daily number that could strand unused
    budget at the end of the period."""
    days_remaining = max(1, (source.period_reset_at - now).days + 1)
    remaining_budget = max(0, monthly_budget - source.calls_used_this_period)
    if remaining_budget <= 0:
        return 0
    return max(1, -(-remaining_budget // days_remaining))  # ceil division


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
    company.recruitee_slug = slugs.get("recruitee")
    company.personio_slug = slugs.get("personio")
    company.board_slugs_checked_at = utcnow()
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
    if ats_type not in ("greenhouse", "lever", "ashby", "recruitee", "personio"):
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
    company.board_slugs_checked_at = utcnow()
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


def _discover_companies_from_jobright(db: Session, settings: GlobalSettings, force: bool = False) -> None:
    """Seeds new Company rows from JobRight's free public job-list repo
    (see jobright_discovery.py's docstring for why this only harvests
    company names, not postings). Gated on its own JobSource-style
    cadence (default 24h, matching how often the underlying repo itself
    updates) so a normal 5-15 min intake tick doesn't refetch a ~300KB
    file for no new data. New companies are seeded with
    board_slugs_checked_at=None, same as any other newly-discovered
    company -- the existing _backfill_board_slugs sweep picks them up
    from there; this function never probes ATS boards itself."""
    source_row = _get_or_create_job_source(db, "jobright")
    if not source_row.is_active:
        return
    now = utcnow()
    if not force and not _is_due(source_row, settings.jobright_poll_interval_hours * 60, now):
        return

    keywords = _get_active_keywords(db)
    try:
        matched_companies = jobright_discovery.fetch_matching_companies(keywords)
    except Exception as e:
        source_row.last_error = str(e)[:250]
        source_row.last_polled_at = now
        db.commit()
        log_activity(db, f"jobright company discovery failed: {e}", "ERROR")
        return

    source_row.last_polled_at = now
    source_row.last_error = None
    db.commit()

    new_count = 0
    for name in matched_companies:
        normalized = normalize_company_name(name)
        exists = db.query(Company).filter(Company.normalized_name == normalized).first()
        if not exists:
            _get_or_create_company(db, name)
            new_count += 1

    log_activity(
        db,
        f"jobright: scanned {len(matched_companies)} keyword-matching compan(ies) from the daily list, "
        f"{new_count} newly discovered (queued for board-slug backfill).",
        "INFO",
    )


def _discover_companies_from_ats_dataset(db: Session, settings: GlobalSettings, force: bool = False) -> None:
    """Seeds new Company rows -- WITH their real ATS slug already
    verified live -- from the ats-scrapers open dataset (see
    ats_dataset_discovery.py's docstring). Unlike jobright's company-
    name-only seeding, a company from here goes straight to a
    board_slugs_checked_at-set, slug-populated state on its very first
    save if the live probe confirms the dataset's slug still works --
    no separate _backfill_board_slugs pass needed for these. Capped at
    settings.bulk_discovery_batch_size new companies per cycle so this
    doesn't spike probe traffic or flood the Company table in one go;
    the dataset itself (~15,500 rows across the 5 supported platforms)
    is re-scanned every cycle this source is due, but only rows not
    already a known Company are acted on, so later cycles naturally
    pick up where earlier ones left off."""
    source_row = _get_or_create_job_source(db, "ats_dataset")
    if not source_row.is_active:
        return
    now = utcnow()
    if not force and not _is_due(source_row, settings.bulk_discovery_poll_interval_hours * 60, now):
        return

    try:
        by_ats = ats_dataset_discovery.fetch_companies_by_ats()
    except Exception as e:
        source_row.last_error = str(e)[:250]
        source_row.last_polled_at = now
        db.commit()
        log_activity(db, f"ATS-dataset company discovery failed: {e}", "ERROR")
        return

    source_row.last_polled_at = now
    source_row.last_error = None
    db.commit()

    # Loaded once up front rather than one query per candidate -- the
    # dataset has ~15,500 rows and this app already tracks hundreds of
    # companies, so an in-memory set beats thousands of repeated
    # indexed lookups every cycle.
    existing_normalized = {row[0] for row in db.query(Company.normalized_name).all()}
    batch_cap = settings.bulk_discovery_batch_size
    candidates = []  # (ats, name, slug)
    seen_this_batch = set()

    # Round-robin across ATS platforms, one candidate at a time, rather
    # than draining them in a fixed order -- Greenhouse alone has
    # thousands more not-yet-seen rows than the other four combined, so
    # a fixed-order greedy fill let it consume the entire per-cycle
    # batch by itself every time, starving Lever/Ashby/Recruitee/
    # Personio completely (confirmed live: 0 Lever candidates ever
    # reached, cycle after cycle, until this was caught and fixed).
    iterators = {ats: iter(pairs) for ats, pairs in by_ats.items()}
    while len(candidates) < batch_cap and iterators:
        exhausted = []
        for ats, it in iterators.items():
            if len(candidates) >= batch_cap:
                break
            for name, slug in it:
                normalized = normalize_company_name(name)
                if normalized in existing_normalized or normalized in seen_this_batch:
                    continue
                seen_this_batch.add(normalized)
                candidates.append((ats, name, slug))
                break
            else:
                exhausted.append(ats)
        for ats in exhausted:
            del iterators[ats]

    if not candidates:
        log_activity(db, "ATS-dataset discovery: no new companies this cycle (all already known).", "INFO")
        return

    # Verification probes are independent network calls -- run
    # concurrently, same rationale as _backfill_board_slugs (SQLAlchemy
    # Sessions aren't thread-safe, so DB writes happen back on this
    # thread, sequentially, once all probes return).
    with ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as pool:
        verified = list(pool.map(lambda c: board_discovery.probe_known_slug(c[0], c[2], c[1]), candidates))

    verified_count = 0
    for (ats, name, slug), is_verified in zip(candidates, verified):
        company = _get_or_create_company(db, name)
        if is_verified:
            setattr(company, f"{ats}_slug", slug)
            company.board_slugs_checked_at = utcnow()
            verified_count += 1
    db.commit()

    log_activity(
        db,
        f"ATS-dataset discovery: {len(candidates)} new compan(ies) seeded this cycle, "
        f"{verified_count} with a live-verified board slug on first save.",
        "INFO",
    )


def _discover_companies_from_job_board_aggregator(db: Session, settings: GlobalSettings, force: bool = False) -> None:
    """Secondary/supplementary to _discover_companies_from_ats_dataset
    -- see job_board_aggregator_discovery.py's docstring for why this
    source only has bare slugs (greenhouse/lever/ashby only, no
    company name, no recruitee/personio coverage). A verified slug
    here is seeded under a slug-derived provisional name (clearly a
    guess) -- same lower-confidence posture board_discovery.py already
    applies to Personio; correctable manually from the Jobs page like
    any other auto-detected slug."""
    source_row = _get_or_create_job_source(db, "job_board_aggregator")
    if not source_row.is_active:
        return
    now = utcnow()
    if not force and not _is_due(source_row, settings.bulk_discovery_poll_interval_hours * 60, now):
        return

    try:
        by_ats = job_board_aggregator_discovery.fetch_slugs_by_ats()
    except Exception as e:
        source_row.last_error = str(e)[:250]
        source_row.last_polled_at = now
        db.commit()
        log_activity(db, f"job-board-aggregator company discovery failed: {e}", "ERROR")
        return

    source_row.last_polled_at = now
    source_row.last_error = None
    db.commit()

    existing_normalized = {row[0] for row in db.query(Company.normalized_name).all()}
    slug_field_map = {"greenhouse": Company.greenhouse_slug, "lever": Company.lever_slug, "ashby": Company.ashby_slug}
    # A slug already attached to a known company (most likely found via
    # ats_dataset_discovery, which has real names) shouldn't be
    # re-seeded here under a worse, slug-derived name.
    existing_slugs_by_ats = {
        ats: {row[0] for row in db.query(field).filter(field.isnot(None)).all()}
        for ats, field in slug_field_map.items()
    }

    batch_cap = settings.bulk_discovery_batch_size
    candidates = []  # (ats, slug)

    # Round-robin across ATS platforms rather than draining them in a
    # fixed order -- same fairness fix as _discover_companies_from_ats_dataset,
    # so Greenhouse's much larger candidate pool doesn't starve Lever/Ashby.
    iterators = {ats: iter(slugs) for ats, slugs in by_ats.items()}
    while len(candidates) < batch_cap and iterators:
        exhausted = []
        for ats, it in iterators.items():
            if len(candidates) >= batch_cap:
                break
            for slug in it:
                if slug in existing_slugs_by_ats[ats]:
                    continue
                provisional_name = job_board_aggregator_discovery.slug_to_provisional_name(slug)
                if normalize_company_name(provisional_name) in existing_normalized:
                    continue
                candidates.append((ats, slug))
                break
            else:
                exhausted.append(ats)
        for ats in exhausted:
            del iterators[ats]

    if not candidates:
        log_activity(db, "job-board-aggregator discovery: no new companies this cycle.", "INFO")
        return

    with ThreadPoolExecutor(max_workers=min(len(candidates), 10)) as pool:
        verified = list(pool.map(lambda c: board_discovery.probe_known_slug(c[0], c[1]), candidates))
    live_candidates = [c for c, is_verified in zip(candidates, verified) if is_verified]

    # For platforms whose API exposes a real employer name alongside
    # the listings (confirmed for Greenhouse -- see fetch_verified_name's
    # docstring), fetch it now rather than falling back to the
    # slug-derived guess; a live board with no such field (Ashby) or an
    # unprobeable one (Lever, currently auth-walled) gets None back and
    # keeps the provisional name.
    with ThreadPoolExecutor(max_workers=min(len(live_candidates), 10)) as pool:
        real_names = list(pool.map(lambda c: board_discovery.fetch_verified_name(c[0], c[1]), live_candidates))

    new_count = 0
    real_name_count = 0
    for (ats, slug), real_name in zip(live_candidates, real_names):
        provisional_name = job_board_aggregator_discovery.slug_to_provisional_name(slug)
        name_to_use = real_name or provisional_name
        normalized = normalize_company_name(name_to_use)
        # A same-cycle collision (two slugs resolving to the same name,
        # or a real name that turns out to already be tracked under a
        # different source) means this company is already accounted
        # for -- skip rather than merge, no postings lost since nothing
        # pointed at this slug before.
        if normalized in existing_normalized or db.query(Company).filter(Company.normalized_name == normalized).first():
            continue
        existing_normalized.add(normalized)
        company = _get_or_create_company(db, name_to_use)
        setattr(company, f"{ats}_slug", slug)
        company.board_slugs_checked_at = utcnow()
        new_count += 1
        if real_name:
            real_name_count += 1
    db.commit()

    log_activity(
        db,
        f"job-board-aggregator discovery: {new_count} new compan(ies) seeded this cycle "
        f"({real_name_count} with a real name recovered from the ATS's own API, "
        f"{new_count - real_name_count} still under a slug-derived placeholder name -- "
        "review/correct those from the Jobs page).",
        "INFO",
    )


def _discover_companies_from_yc_directory(db: Session, settings: GlobalSettings, force: bool = False) -> None:
    """Seeds new Company rows from YC's own public company-directory API
    (see yc_directory_discovery.py's docstring for why this is company
    names only -- workatastartup.com's actual job board needs a login,
    so this uses YC's public directory API instead and lets the ATS
    slug get guessed the normal way, same as jobright). Shares the same
    JobSource-cadence-gated shape as _discover_companies_from_jobright,
    and reuses the bulk-discovery cadence/batch settings the other two
    bulk sources use rather than adding a fourth near-identical
    GlobalSettings pair -- same kind of "large company-name list,
    re-scanned each due cycle, only new rows acted on" pattern."""
    source_row = _get_or_create_job_source(db, "yc_directory")
    if not source_row.is_active:
        return
    now = utcnow()
    if not force and not _is_due(source_row, settings.bulk_discovery_poll_interval_hours * 60, now):
        return

    try:
        names = yc_directory_discovery.fetch_hiring_company_names()
    except Exception as e:
        source_row.last_error = str(e)[:250]
        source_row.last_polled_at = now
        db.commit()
        log_activity(db, f"YC directory company discovery failed: {e}", "ERROR")
        return

    source_row.last_polled_at = now
    source_row.last_error = None
    db.commit()

    existing_normalized = {row[0] for row in db.query(Company.normalized_name).all()}
    batch_cap = settings.bulk_discovery_batch_size
    new_count = 0
    for name in names:
        if new_count >= batch_cap:
            break
        normalized = normalize_company_name(name)
        if normalized in existing_normalized:
            continue
        existing_normalized.add(normalized)
        _get_or_create_company(db, name)
        new_count += 1

    log_activity(
        db,
        f"YC directory: scanned {len(names)} currently-hiring compan(ies), "
        f"{new_count} newly discovered (queued for board-slug backfill).",
        "INFO",
    )


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
            gap = utcnow() - candidate.last_seen_at
            return candidate, gap > timedelta(days=_REPOST_GAP_DAYS)

    return None, False


def _ingest_raw_posting(db: Session, module, raw) -> JobPosting | None:
    company = _get_or_create_company(db, raw.company_name_raw)
    matched, is_repost = _find_matching_posting(db, company.id, raw)

    if matched and not is_repost:
        matched.last_seen_at = utcnow()
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
        first_seen_at=raw.posted_at or utcnow(),
        last_seen_at=utcnow(),
        repost_count=(matched.repost_count + 1) if is_repost else 0,
        scam_flag_reason=_detect_scam_patterns(job_description),
        eligibility_flag_reason=_detect_eligibility_flags(job_description),
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
    if posting.eligibility_flag_reason:
        log_activity(
            db,
            f"Eligibility flag on '{raw.job_title}' at {raw.company_name_raw}: {posting.eligibility_flag_reason}",
            "WARNING",
        )

    return posting


def _quota_cost(module_name: str, keywords: list[str], raw_postings: list) -> int:
    """How much of a source's monthly quota this cycle consumed. Adzuna
    bills per search call (one per keyword); JobsPipe bills per job
    actually returned ("1 credit = 1 job") regardless of how many
    keywords were searched -- these need different accounting even
    though both share the same budget/pacing machinery."""
    if module_name == jobspipe_source.SOURCE_NAME:
        return len(raw_postings)
    return len(keywords)


def _run_source(db: Session, module, source_row: JobSource, location_query: str) -> None:
    now = utcnow()

    if not module.is_configured():
        if source_row.last_error != "not_configured":
            log_activity(db, f"Skipping {module.SOURCE_NAME}: not configured (missing credentials).", "WARNING")
        source_row.last_error = "not_configured"
        source_row.last_polled_at = now
        db.commit()
        return

    _reset_call_period_if_needed(source_row, now)
    monthly_budget = _QUOTA_BUDGETED_SOURCES.get(module.SOURCE_NAME)
    if monthly_budget is not None:
        if source_row.calls_used_this_period >= monthly_budget:
            log_activity(db, f"Skipping {module.SOURCE_NAME}: monthly quota reached.", "WARNING")
            db.commit()
            return
        # Daily pacing: a normal polling cadence can burn the whole
        # monthly budget in the first day or two and then go dark for
        # the rest of the period (confirmed for real at this project's
        # own defaults -- see JobSource.calls_used_today's docstring).
        # Spreading it evenly means coverage stays available all month
        # instead of front-loaded into the first couple of days.
        _reset_daily_counter_if_needed(source_row, now)
        daily_budget = _adzuna_daily_budget(source_row, now, monthly_budget)
        if source_row.calls_used_today >= daily_budget:
            log_activity(
                db,
                f"Skipping {module.SOURCE_NAME}: today's pacing budget ({daily_budget}) already used.",
                "INFO",
            )
            db.commit()
            return

    all_keywords = _get_active_keywords(db)
    # Adzuna and LinkedIn cost one real external call per keyword, per
    # cycle -- rotate through a bounded subset so a large keyword list
    # can't exhaust a whole budget period in a single cycle. Greenhouse/
    # Lever/Ashby fetch a company's whole board in one call regardless
    # of keyword count and filter titles locally, so they always get
    # the full list.
    if module.SOURCE_NAME in _PER_KEYWORD_CALL_SOURCES:
        keywords, next_offset = _rotating_keyword_subset(
            all_keywords, source_row.keyword_rotation_offset, _KEYWORDS_PER_CYCLE
        )
        source_row.keyword_rotation_offset = next_offset
    else:
        keywords = all_keywords

    try:
        raw_postings = module.cheap_scan(keywords, location_query, limit=15)
    except Exception as e:
        source_row.last_error = str(e)[:250]
        source_row.last_polled_at = now
        db.commit()
        log_activity(db, f"{module.SOURCE_NAME} intake failed: {e}", "ERROR")
        return

    quota_units = _quota_cost(module.SOURCE_NAME, keywords, raw_postings)
    source_row.calls_used_this_period += quota_units
    if module.SOURCE_NAME in _QUOTA_BUDGETED_SOURCES:
        source_row.calls_used_today += quota_units
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
    cutoff = utcnow() - timedelta(days=threshold_days)
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


def _propose_intake_targeting(profile_content: dict) -> dict:
    llm = get_llm_provider()
    raw = llm.complete_json(
        system=(
            "You are an expert technical recruiter helping a candidate configure automated job-search "
            "intake. You return only raw JSON."
        ),
        prompt=(
            "Based on this candidate's real profile, propose job-title search keywords and a seniority "
            "exclusion list for automated job intake.\n\n"
            "Keywords: propose 15-20 distinct job title PATTERNS a recruiter would plausibly use for roles "
            "this candidate is genuinely qualified for, given their actual skills and experience. Each "
            "keyword becomes a real, separate call against a rate-limited external search API, so the "
            "count matters -- do not propose industry- or domain-flavored variants of a title already "
            "covered by a broader keyword (e.g. do not add 'Healthcare Data Analyst' and 'Retail Data "
            "Analyst' as separate keywords if plain 'Data Analyst' is already in the list -- a broader "
            "keyword already matches those titles). Prioritize genuinely distinct title patterns and real "
            "synonyms recruiters actually use (e.g. 'ML Engineer' vs 'Data Scientist' vs 'AI Engineer' are "
            "genuinely different searches worth having separately) over exhaustive domain enumeration. "
            "Every keyword must still be genuinely grounded in the candidate's real background, not "
            "aspirational reach titles.\n\n"
            "Seniority exclusions: title-level terms (e.g. 'Staff', 'Director', 'VP') to exclude, matching "
            "what this candidate's real years of experience and title history actually support. An "
            "early-career profile should exclude senior/staff/manager/director-level terms; a genuinely "
            "senior profile should exclude few or none.\n\n"
            f"Candidate Profile:\n{json.dumps(profile_content, indent=2)}\n\n"
            'Respond with EXACTLY this JSON shape: {"keywords": ["...", "..."], "seniority_exclusions": '
            '["...", "..."]}\n'
            "Do not wrap the output in markdown code fences."
        ),
        temperature=0.3,
    )
    return parse_json_response(raw)


def ensure_intake_targeting(db: Session) -> None:
    """Self-healing check, run at the top of every intake cycle: if
    search keywords or seniority exclusions are empty, derive them from
    the candidate's real active profile instead of silently running
    intake with nothing configured (or everything unfiltered by
    seniority). Never touches a table that already has rows -- this
    only fills a genuinely empty config, never overwrites a human's own
    curated list.

    This exists because it already happened once for real: after a
    database migration, the search-keyword table ended up empty with no
    error or warning anywhere, and intake silently found nothing new for
    days before it was noticed."""
    has_keywords = db.query(SearchKeyword).count() > 0
    has_exclusions = db.query(SeniorityExclusion).count() > 0
    if has_keywords and has_exclusions:
        return

    profile_content = get_default_profile_content(db)
    if not profile_content:
        log_activity(
            db,
            "Intake has no search keywords configured and no profile to derive them from yet -- "
            "seed a profile on the Profile page to auto-configure intake targeting.",
            "WARNING",
        )
        return

    try:
        proposed = _propose_intake_targeting(profile_content)
    except Exception as e:
        log_activity(db, f"Failed to auto-derive intake targeting from profile: {e}", "ERROR")
        return

    # Hard cap regardless of what the LLM actually returned -- each
    # keyword is a real, separate call against Adzuna's rate-limited
    # search API (see _ADZUNA_MONTHLY_CALL_BUDGET), so an unbounded list
    # isn't just a quality issue, it can exhaust a whole month's budget
    # in a single intake cycle.
    keywords_added = 0
    if not has_keywords:
        for kw in proposed.get("keywords", [])[:_MAX_AUTO_KEYWORDS]:
            if kw and not db.query(SearchKeyword).filter(SearchKeyword.keyword == kw).first():
                db.add(SearchKeyword(keyword=kw, is_active=True))
                keywords_added += 1
    exclusions_added = 0
    if not has_exclusions:
        for term in proposed.get("seniority_exclusions", []):
            if term and not db.query(SeniorityExclusion).filter(SeniorityExclusion.term == term).first():
                db.add(SeniorityExclusion(term=term, is_active=True))
                exclusions_added += 1
    db.commit()

    log_activity(
        db,
        f"Auto-configured intake targeting from your active profile: "
        f"{keywords_added} keyword(s), {exclusions_added} seniority exclusion(s). "
        "Review and adjust on the Jobs page any time.",
        "INFO",
    )


def ensure_location_exclusions_seeded(db: Session) -> None:
    """Same never-touch-a-configured-table rule as ensure_intake_targeting,
    but simpler: no LLM call, just a one-time static seed the first time
    the table is found empty. Direct-board intake (Greenhouse/Lever/Ashby)
    has no location search param at all, so without this a US-based
    candidate's intake fills up with roles open only in other countries --
    confirmed for real (an Affirm "Remote Poland"-only posting reached
    scoring/tailoring before this existed)."""
    if db.query(LocationExclusion).count() > 0:
        return
    for term in _DEFAULT_LOCATION_EXCLUSIONS:
        db.add(LocationExclusion(term=term, is_active=True))
    db.commit()
    log_activity(
        db,
        f"Seeded {len(_DEFAULT_LOCATION_EXCLUSIONS)} default location exclusion(s) -- "
        "review and adjust on the Jobs page any time.",
        "INFO",
    )


def run_intake_cycle(db: Session, force: bool = False) -> None:
    """Entry point called by the scheduler, and by the manual 'run now'
    trigger with force=True. Polls whichever sources are due, respecting
    the kill switch, each source's own cadence, and each quota-budgeted
    source's own call/job budget (see _QUOTA_BUDGETED_SOURCES).
    force=True bypasses the cadence check (so a manual click actually
    does something instead of silently no-op'ing if a source was already
    polled recently) but still respects those hard monthly budget caps
    -- those are real quota limits, not just politeness."""
    settings = get_or_create_settings(db)
    if not settings.automation_enabled:
        return

    ensure_intake_targeting(db)
    ensure_location_exclusions_seeded(db)

    now = utcnow()
    location_query = settings.location_query or "United States"

    linkedin_row = _get_or_create_job_source(db, linkedin_source.SOURCE_NAME)
    if linkedin_row.is_active and (force or _is_due(linkedin_row, settings.fast_poll_interval_minutes, now)):
        _run_source(db, linkedin_source, linkedin_row, location_query)

    adzuna_row = _get_or_create_job_source(db, adzuna_source.SOURCE_NAME)
    if adzuna_row.is_active and (force or _is_due(adzuna_row, settings.full_ingest_interval_minutes, now)):
        _run_source(db, adzuna_source, adzuna_row, location_query)

    # Direct-ATS sources: no search quota to respect,
    # same low-indexing-lag rationale as LinkedIn -- poll at the fast
    # cadence. Each is a no-op (skipped with a clear log entry) until at
    # least one Company row has that ATS's slug set. location_query is
    # unused by these five (no location search param on any of their
    # APIs) -- they filter locally via LocationExclusion instead.
    for module in (greenhouse_source, lever_source, ashby_source, recruitee_source, personio_source):
        source_row = _get_or_create_job_source(db, module.SOURCE_NAME)
        if source_row.is_active and (force or _is_due(source_row, settings.fast_poll_interval_minutes, now)):
            _run_source(db, module, source_row, location_query)

    # JobsPipe: a broad-net normalizing aggregator (Workday/iCIMS/
    # SmartRecruiters/etc, already tenant-mapped on their end) -- same
    # budget-capped shape as Adzuna (see _QUOTA_BUDGETED_SOURCES), not a
    # per-company direct-ATS source, so it's polled on the slower
    # full-ingest cadence like Adzuna rather than the fast cadence above.
    jobspipe_row = _get_or_create_job_source(db, jobspipe_source.SOURCE_NAME)
    if jobspipe_row.is_active and (force or _is_due(jobspipe_row, settings.full_ingest_interval_minutes, now)):
        _run_source(db, jobspipe_source, jobspipe_row, location_query)

    _discover_companies_from_jobright(db, settings, force=force)
    _discover_companies_from_ats_dataset(db, settings, force=force)
    _discover_companies_from_job_board_aggregator(db, settings, force=force)
    _discover_companies_from_yc_directory(db, settings, force=force)
    _backfill_board_slugs(db)
    _flag_stale_postings(db, settings.stale_posting_threshold_days)
