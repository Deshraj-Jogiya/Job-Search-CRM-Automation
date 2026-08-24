"""
Direct Ashby job board intake -- same shape as
greenhouse_source.py/lever_source.py, driven by Company rows with an
ashby_slug set. Ashby's public job-board API (https://api.ashbyhq.com/
posting-api/job-board/{slug}) returns the full description in the same
listing call, no separate per-posting fetch needed.
"""

import html
import re

import requests
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "ashby"

_TIMEOUT = 10


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.ashby_slug.isnot(None), Company.status != "Blocked")
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
                f"https://api.ashbyhq.com/posting-api/job-board/{company.ashby_slug}",
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
            location = job.get("location")
            if not location_allowed(location, location_exclusions):
                continue
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=job.get("id") or None,
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=job.get("jobUrl", ""),
                    job_description=_clean_html(job.get("descriptionHtml", "") or job.get("description", "")) or None,
                    location=location,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    return posting.job_description or ""
