"""Simulated job intake for the public showcase instance.

The showcase polls no real job board (every source is prod_only, see source_visibility.py), so "Run intake" there
draws from a fixed pool of fictional postings instead. Each one goes through the same steps a real listing does: the
title must match an active search keyword and no seniority exclusion, the location must pass the location
exclusions, the dedupe in intake_service decides whether it is new, and a new one is scored by the same rules. Some
pool entries are meant to be filtered (a Staff title, a London role, a frontend role, a repost of a tracked posting),
so the counts a visitor sees come from the filters, not from a script. The daily demo reset clears what was added.
"""
import random
from types import SimpleNamespace

from sqlalchemy.orm import Session

from ..app_mode import is_showcase_mode
from ..models import ActivityLog, JobApplication, JobPosting, LocationExclusion, SearchKeyword, SeniorityExclusion
from .activity_logger import log_activity
from .sources.base import RawPosting
from .sources.keyword_matching import location_allowed, matches_keywords, matches_seniority_exclusion

DEMO_SEARCH_KEYWORDS = ["Data Engineer", "Analytics Engineer", "Data Platform Engineer", "ETL Developer"]
DEMO_SENIORITY_EXCLUSIONS = ["Staff", "Principal", "Director", "Head of", "Intern"]

_JD = (
    "About the team\n{team}\n\n"
    "What you'll do\n"
    "- Build and run the pipelines that feed {consumers}.\n"
    "- Model data in the warehouse and keep it tested, documented and fresh.\n"
    "- Add data quality checks and alerts, and follow incidents through to a fix.\n\n"
    "What we're looking for\n"
    "- {years}+ years building production data pipelines in Python and SQL.\n"
    "- Hands-on experience with {stack}.\n"
    "- Comfort with {cloud}.\n\n"
    "Compensation\nThe base salary range for this role is ${lo:,} - ${hi:,}. {sponsor}"
)

# company, title, source, location, team, consumers, years, stack, cloud, lo, hi, sponsor sentence
_POOL = [
    ("Harborview Freight", "Data Engineer", "greenhouse", "Seattle, WA (Hybrid)",
     "Our data team tracks every container from port to warehouse.", "dock scheduling and demurrage reports", 3,
     "Airflow, dbt and Snowflake", "AWS", 128000, 156000, "We sponsor H-1B visas for this role."),
    ("Cedar Ridge Credit Union", "Analytics Engineer", "lever", "Remote (US)",
     "A member-owned credit union building its first modern data stack.", "member and loan dashboards", 2,
     "dbt, SQL and Looker", "Google Cloud", 105000, 128000, ""),
    ("Quillmark Publishing", "Data Platform Engineer", "ashby", "New York, NY",
     "We run the platform behind 40 magazine and newsletter brands.", "subscription and ad revenue models", 4,
     "Spark, Kafka and Delta Lake", "AWS and Terraform", 145000, 175000, "This position is eligible for H-1B sponsorship."),
    ("Mesa Solar Cooperative", "Data Engineer II", "workable", "Tempe, AZ",
     "We forecast output for 1,200 rooftop arrays across Arizona.", "generation forecasts and billing", 3,
     "Python, Airflow and PostgreSQL", "Azure", 112000, 134000, "We sponsor H-1B visas for this role."),
    ("Bluebird Pet Health", "ETL Developer", "smartrecruiters", "Denver, CO (Hybrid)",
     "Clinic visit and pharmacy data for 300 veterinary clinics.", "clinic operations reporting", 2,
     "SQL Server, SSIS and Python", "Azure", 95000, 115000, ""),
    ("Tidewater Analytics", "Senior Analytics Engineer", "greenhouse", "Remote (US)",
     "Analytics consulting for regional hospital groups.", "readmission and staffing models", 5,
     "dbt, Snowflake and Airflow", "AWS", 150000, 178000, "We sponsor H-1B visas for this role."),
    ("Granite Peak Outdoor", "Data Engineer", "recruitee", "Salt Lake City, UT",
     "Ecommerce and store data for an outdoor gear retailer.", "inventory and demand planning", 2,
     "BigQuery, dbt and Python", "Google Cloud", 104000, 126000, ""),
    ("Sundial Insurance", "Data Engineer", "lever", "Phoenix, AZ (Hybrid)",
     "Claims and policy data for a regional auto insurer.", "pricing and fraud models", 3,
     "Spark, Python and Redshift", "AWS", 118000, 142000, "We sponsor H-1B visas for this role."),
    ("Arclight Robotics", "Data Platform Engineer", "ashby", "Pittsburgh, PA",
     "Telemetry from 900 warehouse robots, one event every 50 ms.", "fleet health dashboards", 4,
     "Kafka, Flink and ClickHouse", "AWS and Kubernetes", 150000, 185000, "This position is eligible for H-1B sponsorship."),
    ("Prairie Grain Exchange", "Analytics Engineer", "personio", "Chicago, IL",
     "Market and settlement data for a grain trading exchange.", "settlement and risk reports", 3,
     "dbt, PostgreSQL and Airflow", "AWS", 120000, 145000, ""),
    # Entries the filters are expected to stop.
    ("Arclight Robotics", "Staff Data Engineer", "ashby", "Pittsburgh, PA",
     "Telemetry from 900 warehouse robots.", "fleet health dashboards", 9,
     "Kafka, Flink and ClickHouse", "AWS", 200000, 240000, ""),
    ("Quillmark Publishing", "Data Engineer", "ashby", "London, United Kingdom",
     "The UK edition team.", "UK subscription reporting", 3,
     "Spark and Delta Lake", "AWS", 70000, 85000, ""),
    ("Harborview Freight", "Frontend Engineer", "greenhouse", "Seattle, WA",
     "The dispatcher web app team.", "the dispatcher web app", 3,
     "React and TypeScript", "AWS", 130000, 150000, ""),
    ("Sundial Insurance", "Director of Data Engineering", "lever", "Phoenix, AZ",
     "Leads the claims data group.", "the claims data group", 12,
     "Spark and Redshift", "AWS", 210000, 250000, ""),
    ("Northwind Logistics", "Senior Data Engineer", "greenhouse", "Austin, TX (Hybrid)",
     "Repost of a role this search already tracks.", "dispatch dashboards and ETA models", 5,
     "Airflow, Spark and Kafka", "AWS", 150000, 182000, "We sponsor H-1B visas for this role."),
    ("Mesa Solar Cooperative", "Data Engineer", "workable", "Bengaluru, India",
     "The India engineering center.", "generation forecasts", 3,
     "Python and Airflow", "Azure", 30000, 40000, ""),
]

_SOURCE = SimpleNamespace(fetch_full_description=lambda raw: None)


def ensure_demo_search_settings(db: Session) -> None:
    """The keywords and exclusions a real profile's first intake run would set up (empty tables only)."""
    from .intake_service import ensure_location_exclusions_seeded

    if db.query(SearchKeyword).count() == 0:
        for k in DEMO_SEARCH_KEYWORDS:
            db.add(SearchKeyword(keyword=k, is_active=True))
    if db.query(SeniorityExclusion).count() == 0:
        for t in DEMO_SENIORITY_EXCLUSIONS:
            db.add(SeniorityExclusion(term=t, is_active=True))
    db.commit()
    ensure_location_exclusions_seeded(db)


def _raw(i: int, p: tuple) -> RawPosting:
    return RawPosting(source=p[2], company_name_raw=p[0], job_title=p[1], job_url=None, external_id=f"demo-pool-{i}",
                      location=p[3], job_description=_JD.format(team=p[4], consumers=p[5], years=p[6], stack=p[7],
                                                                cloud=p[8], lo=p[9], hi=p[10], sponsor=p[11]).strip())


def run_simulated_intake(db: Session, batch_size: int = 6, rng: random.Random | None = None) -> dict:
    """Pull up to `batch_size` unseen pool postings through the real filters, dedupe and scoring. Showcase only."""
    if not is_showcase_mode():
        raise RuntimeError("Simulated intake runs only on the showcase instance.")
    from .intake_service import _ingest_raw_posting
    from .matching_service import score_application

    ensure_demo_search_settings(db)
    rng = rng or random.Random()
    seen = {e for (e,) in db.query(JobPosting.external_id).filter(JobPosting.external_id.like("demo-pool-%"))}
    pulled_ids = {int(e.rsplit("-", 1)[1]) for e in seen if e}
    pulled_ids |= set(_pulled_but_filtered(db))
    unseen = [i for i in range(len(_POOL)) if i not in pulled_ids]
    batch = rng.sample(unseen, min(batch_size, len(unseen)))

    keywords = [k.keyword for k in db.query(SearchKeyword).filter(SearchKeyword.is_active == True)]  # noqa: E712
    seniority = [s.term for s in db.query(SeniorityExclusion).filter(SeniorityExclusion.is_active == True)]  # noqa: E712
    locations = [l.term for l in db.query(LocationExclusion).filter(LocationExclusion.is_active == True)]  # noqa: E712

    counts = {"fetched": len(batch), "title": 0, "seniority": 0, "location": 0, "duplicate": 0, "added": 0}
    added = []
    for i in batch:
        raw = _raw(i, _POOL[i])
        if not matches_keywords(raw.job_title, keywords):
            reason = "title"
        elif matches_seniority_exclusion(raw.job_title, seniority):
            reason = "seniority"
        elif not location_allowed(raw.location, locations):
            reason = "location"
        else:
            posting = _ingest_raw_posting(db, _SOURCE, raw)
            if posting is None:
                reason = "duplicate"
            else:
                counts["added"] += 1
                added.append(posting)
                continue
        counts[reason] += 1
        log_activity(db, f"Demo intake: skipped '{raw.job_title}' at {raw.company_name_raw} ({_REASONS[reason]}) "
                         f"[demo-pool-{i}]", "INFO")

    for posting in added:
        app = db.query(JobApplication).filter(JobApplication.posting_id == posting.id).first()
        score_application(db, app.id)

    counts["left_in_pool"] = len(unseen) - len(batch)
    log_activity(db, f"Demo intake: {summary_text(counts)}", "INFO")
    return counts


_REASONS = {"title": "title matches no search keyword", "seniority": "seniority exclusion",
            "location": "location exclusion", "duplicate": "already tracked"}


def _pulled_but_filtered(db: Session) -> list[int]:
    """Pool entries a previous run already filtered out, read back from that run's own activity log lines."""
    ids = []
    for (msg,) in db.query(ActivityLog.message).filter(ActivityLog.message.like("Demo intake: skipped %[demo-pool-%]")):
        try:
            ids.append(int(msg.rsplit("[demo-pool-", 1)[1].rstrip("]")))
        except (IndexError, ValueError):
            pass
    return ids


def summary_text(counts: dict) -> str:
    if counts["fetched"] == 0:
        return "the sample pool is used up for today; it refills at the daily reset."
    skipped = [f"{counts[k]} {label}" for k, label in
               (("title", "off-target title"), ("seniority", "too senior"), ("location", "outside the US"),
                ("duplicate", "already tracked")) if counts[k]]
    text = f"{counts['fetched']} fetched, {counts['added']} new and scored by the rules"
    if skipped:
        text += f"; filtered: {', '.join(skipped)}"
    return text + f". {counts['left_in_pool']} sample postings left in the pool."
