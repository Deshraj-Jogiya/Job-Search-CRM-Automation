"""
Direct Lever job board intake (Phase 2 slice 2) -- same shape as
greenhouse_source.py, driven by Company rows with a lever_slug set.
Lever's public postings API (https://api.lever.co/v1/postings/{slug}
?mode=json) returns the full description in the same listing call, no
separate per-posting fetch needed.
"""

import html
import re

import requests
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting

SOURCE_NAME = "lever"

_TIMEOUT = 10


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.lever_slug.isnot(None), Company.status != "Blocked")
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
                f"https://api.lever.co/v1/postings/{company.lever_slug}",
                params={"mode": "json"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            jobs = resp.json()
        except Exception:
            continue

        for job in jobs:
            title = job.get("text", "")
            if not title or not _matches_keywords(title, keywords):
                continue
            description_html = (job.get("description") or "") + "".join(
                lst.get("content", "") for lst in job.get("lists", [])
            )
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=job.get("id") or None,
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=job.get("hostedUrl", ""),
                    job_description=_clean_html(description_html) or None,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    return posting.job_description or ""
