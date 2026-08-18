"""
Direct Greenhouse job board intake (Phase 2 slice 2). Unlike LinkedIn/
Adzuna, this isn't a keyword search across all employers -- Greenhouse's
public board API is per-company (https://boards-api.greenhouse.io/v1/
boards/{slug}/jobs), so this source is driven by which Company rows
have a greenhouse_slug set (auto-detected by board_discovery.py, or set
manually from the Jobs page) rather than by a global search term.

This is exactly the low-indexing-lag source CLAUDE.md prioritizes --
Greenhouse's own board reflects a new posting the moment the employer
publishes it, no aggregator re-crawl delay. `?content=true` returns the
full HTML job description in the same listing call, so (like Adzuna,
unlike LinkedIn) there's no separate per-posting fetch needed.
"""

import html
import re

import requests
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting

SOURCE_NAME = "greenhouse"

_TIMEOUT = 10


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.greenhouse_slug.isnot(None), Company.status != "Blocked")
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


def _matches_keywords(title: str, keywords: list[str]) -> bool:
    lower_title = title.lower()
    return any(kw.lower() in lower_title for kw in keywords)


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    db = SessionLocal()
    try:
        companies = _active_target_companies(db)
    finally:
        db.close()

    postings: list[RawPosting] = []
    for company in companies:
        try:
            resp = requests.get(
                f"https://boards-api.greenhouse.io/v1/boards/{company.greenhouse_slug}/jobs",
                params={"content": "true"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        except Exception:
            continue

        for job in jobs:
            title = job.get("title", "")
            if not title or not _matches_keywords(title, keywords):
                continue
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=str(job.get("id")) if job.get("id") else None,
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=job.get("absolute_url", ""),
                    job_description=_clean_html(job.get("content", "")) or None,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan already requests full content (?content=true) --
    # nothing further to fetch.
    return posting.job_description or ""
