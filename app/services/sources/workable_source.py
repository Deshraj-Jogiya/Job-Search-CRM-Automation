"""
Direct Workable job board intake -- same shape as greenhouse_source.py/
ashby_source.py, driven by Company rows with a workable_slug set (see
board_discovery.py's _probe_workable). Workable's public widget API
(apply.workable.com/api/v1/widget/accounts/{slug}?details=true) returns
the full HTML description in the same listing call, no separate
per-posting fetch needed -- same as Greenhouse's ?content=true.
"""

import html
import re

import requests
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "workable"

_TIMEOUT = 10


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.workable_slug.isnot(None), Company.status != "Blocked")
        .all()
    )


def is_configured() -> bool:
    db = SessionLocal()
    try:
        return len(_active_target_companies(db)) > 0
    finally:
        db.close()


def _clean_html(raw_html: str) -> str:
    text = re.sub(r"<.*?>", " ", raw_html or "")
    return html.unescape(text)


def _job_location(job: dict) -> str | None:
    parts = [job.get("city"), job.get("state"), job.get("country")]
    return ", ".join(p for p in parts if p) or None


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    db = SessionLocal()
    try:
        companies = _active_target_companies(db)
    finally:
        db.close()

    exclusions = get_active_seniority_exclusions()
    location_exclusions = get_active_location_exclusions()
    postings: list[RawPosting] = []
    for company in companies:
        try:
            resp = requests.get(
                f"https://apply.workable.com/api/v1/widget/accounts/{company.workable_slug}",
                params={"details": "true"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        except Exception:
            continue

        for job in jobs:
            title = job.get("title", "")
            if not title or not posting_matches(title, keywords, exclusions):
                continue
            job_location = _job_location(job)
            if not location_allowed(job_location, location_exclusions):
                continue
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=job.get("shortcode"),
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=job.get("shortlink") or job.get("url", ""),
                    job_description=_clean_html(job.get("description", "")) or None,
                    location=job_location,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan already requests full content (?details=true) --
    # nothing further to fetch.
    return posting.job_description or ""
