"""
Working Nomads job board (workingnomads.com) -- same fetch-everything-
and-filter-locally shape as RemoteOK/WeWorkRemotely/Jobspresso (no
server-side keyword search param), added to the same Phase 2b
remote-board family rather than its own new cadence/settings.

Real public JSON API, confirmed live 2026-09-11: `/api/exposed_jobs/`
is linked directly from the site's own footer next to their Terms of
Service (not a hidden/undocumented endpoint) and returns a plain JSON
array -- title, full HTML description, company_name, category_name,
tags, location, pub_date, all in one call, no second per-posting fetch
needed. Their Terms of Service (checked live the same day) don't
mention API usage, rate limits, or attribution requirements at all --
the only relevant clause is a general "don't build a competitive
product" restriction, which a personal, non-commercial job tracker
doesn't implicate. No published rate-limit advisory exists (unlike
Remotive's explicit "max 4x/day"), so this follows the same
conservative shared cadence as the other 3 fetch-everything sources
rather than assuming an unstated limit doesn't exist.
"""

import html
import re
from datetime import datetime

import requests

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "workingnomads"

_TIMEOUT = 10
_API_URL = "https://www.workingnomads.com/api/exposed_jobs/"


def is_configured() -> bool:
    return True  # public API, no credentials needed


def _clean_html(raw_html: str) -> str:
    text = re.sub(r"<.*?>", " ", raw_html or "")
    return html.unescape(text)


def _parse_posted_at(date_str: str) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.fromisoformat(date_str).replace(tzinfo=None)
    except ValueError:
        return None


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    exclusions = get_active_seniority_exclusions()
    location_exclusions = get_active_location_exclusions()

    try:
        resp = requests.get(_API_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=_TIMEOUT)
        resp.raise_for_status()
        jobs = resp.json()
    except Exception:
        return []

    postings: list[RawPosting] = []
    for job in jobs:
        title = job.get("title", "")
        job_url = job.get("url", "")
        if not title or not job_url:
            continue
        if not posting_matches(title, keywords, exclusions):
            continue
        job_location = job.get("location") or None
        if not location_allowed(job_location, location_exclusions):
            continue
        postings.append(
            RawPosting(
                source=SOURCE_NAME,
                external_id=job_url,  # no numeric id field in this API -- the url is the stable identifier
                company_name_raw=job.get("company_name", "Unknown Company"),
                job_title=title,
                job_url=job_url,
                job_description=_clean_html(job.get("description", "")) or None,
                posted_at=_parse_posted_at(job.get("pub_date", "")),
                location=job_location,
            )
        )
        if len(postings) >= limit:
            break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan already fetches the full description in one call.
    return posting.job_description or ""
