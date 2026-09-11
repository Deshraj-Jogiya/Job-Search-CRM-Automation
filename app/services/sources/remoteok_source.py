"""
RemoteOK job board (remoteok.com) -- a broad, keyword-search-style
aggregator across all employers, same shape as Adzuna/LinkedIn rather
than a per-company direct-ATS board. Its public API
(remoteok.com/api) has no server-side search param, so every call
returns the full current listing and this module filters locally by
title/seniority, same as the direct-ATS board sources.

RemoteOK's own API response embeds its terms directly as the FIRST
array element (not a real job -- a {"legal": "..."} object): link
back to the RemoteOK URL (with a followed link, not nofollow) and
name RemoteOK as the source, or they'll suspend API access. This app
already links every posting back to its real source URL
(job_url, shown throughout the Jobs/application-detail pages) and
names the source (posting.source == "remoteok" is shown as a badge)
-- satisfied by existing behavior, not anything new needed here.
"""

import html
import re
from datetime import datetime

import requests

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "remoteok"

_TIMEOUT = 10
_API_URL = "https://remoteok.com/api"


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
        title = job.get("position", "")
        # The first array element is RemoteOK's own terms-of-service
        # notice, not a job -- it has no "position"/"id" field, so
        # filtering on a non-empty title already skips it cleanly.
        if not title or not job.get("id"):
            continue
        if not posting_matches(title, keywords, exclusions):
            continue
        job_location = job.get("location") or None
        if not location_allowed(job_location, location_exclusions):
            continue
        postings.append(
            RawPosting(
                source=SOURCE_NAME,
                external_id=str(job.get("id")),
                company_name_raw=job.get("company", "Unknown Company"),
                job_title=title,
                job_url=job.get("url") or job.get("apply_url", ""),
                job_description=_clean_html(job.get("description", "")) or None,
                posted_at=_parse_posted_at(job.get("date", "")),
                location=job_location,
            )
        )
        if len(postings) >= limit:
            break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan already fetches the full description in one call.
    return posting.job_description or ""
