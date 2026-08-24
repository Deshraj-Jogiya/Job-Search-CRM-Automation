"""
Direct Personio job board intake -- same shape as
greenhouse_source.py/lever_source.py/ashby_source.py, driven by Company
rows with a personio_slug set. Personio's public careers-site feed
(https://{slug}.jobs.personio.{com,de}/xml) is XML, not JSON, and
returns the full description inline (like Ashby/Greenhouse) when the
employer has filled one in -- some employers leave it blank, in which
case the posting is simply too short to pass intake_service's
minimum-length check and gets skipped, same as any other source's thin
listing.

Personio splits customers across two TLDs (.com and .de) with no way to
tell which one a given tenant uses from the slug alone, and
personio_slug only stores the bare slug (see board_discovery.py) --
so both are tried here, same pattern the probe itself uses.
"""

import html
import re

import requests
from defusedxml import ElementTree
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "personio"

_TIMEOUT = 10
_TLDS = ("com", "de")


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.personio_slug.isnot(None), Company.status != "Blocked")
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


def _fetch_positions(slug: str) -> list:
    for tld in _TLDS:
        try:
            resp = requests.get(f"https://{slug}.jobs.personio.{tld}/xml", timeout=_TIMEOUT)
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
            positions = root.findall("position")
            if positions:
                return [(tld, p) for p in positions]
        except Exception:
            continue
    return []


def _extract_description(position) -> str:
    blocks = position.find("jobDescriptions")
    if blocks is None:
        return ""
    parts = []
    for block in blocks.findall("jobDescription"):
        value = block.findtext("value") or ""
        if value:
            parts.append(value)
    return _clean_html(" ".join(parts))


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
        for tld, position in _fetch_positions(company.personio_slug):
            title = position.findtext("name") or ""
            if not title or not posting_matches(title, keywords, exclusions):
                continue
            office = position.findtext("office")
            if not location_allowed(office, location_exclusions):
                continue
            job_id = position.findtext("id")
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=job_id or None,
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=f"https://{company.personio_slug}.jobs.personio.{tld}/job/{job_id}" if job_id else "",
                    job_description=_extract_description(position) or None,
                    location=office,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # The XML feed already carries the full description inline when the
    # employer filled one in -- nothing further to fetch.
    return posting.job_description or ""
