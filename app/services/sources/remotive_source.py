"""
Remotive job board (remotive.com) -- unlike the other 3 Phase 2b
sources, its public API has a real server-side `search` param, so
this follows Adzuna's per-keyword-call shape rather than the
fetch-everything-and-filter-locally shape RemoteOK/WeWorkRemotely/
Jobspresso use.

Two real constraints from Remotive's own API terms (confirmed live,
2026-09-10, embedded directly in every API response's
"0-legal-notice" field):
  - Data is delayed 24 hours from what's live on remotive.com itself
    -- this source is NOT the low-indexing-lag kind this app
    otherwise prioritizes, it's a slower supplementary source.
  - "We advise max. 4 times a day" -- intake_service is responsible
    for pacing this (see remote_board_poll_interval_minutes in
    GlobalSettings, shared across all 4 Phase 2b sources), same
    division of responsibility Adzuna's own module docstring
    describes for its quota.
  - Must link back to the Remotive URL and name Remotive as the
    source -- already satisfied by this app's existing job_url +
    source-badge display, same as every other source.
"""

import html
import re
from datetime import datetime

import requests

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, location_allowed

SOURCE_NAME = "remotive"

_TIMEOUT = 10
_API_URL = "https://remotive.com/api/remote-jobs"


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
    location_exclusions = get_active_location_exclusions()
    postings: list[RawPosting] = []

    for keyword in keywords:
        try:
            resp = requests.get(_API_URL, params={"search": keyword, "limit": limit}, timeout=_TIMEOUT)
            resp.raise_for_status()
            jobs = resp.json().get("jobs", [])
        except Exception:
            continue

        for job in jobs:
            job_url = job.get("url", "")
            if not job_url:
                continue
            job_location = job.get("candidate_required_location")
            if not location_allowed(job_location, location_exclusions):
                continue
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=str(job.get("id", "")) or None,
                    company_name_raw=job.get("company_name", "Unknown Company"),
                    job_title=job.get("title", ""),
                    job_url=job_url,
                    job_description=_clean_html(job.get("description", "")) or None,
                    posted_at=_parse_posted_at(job.get("publication_date", "")),
                    location=job_location,
                )
            )

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan already fetches the full description in one call.
    return posting.job_description or ""
