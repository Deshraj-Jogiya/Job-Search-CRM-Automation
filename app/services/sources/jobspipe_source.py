"""
JobsPipe -- a normalizing job-board aggregator covering 30+ ATS
platforms (including Workday, iCIMS, SmartRecruiters, Taleo,
SuccessFactors) already tenant-mapped on their end, requires
JOBSPIPE_API_KEY. This exists specifically to reach the large-enterprise
employers Greenhouse/Lever/Ashby/Recruitee/Personio structurally can't
(those platforms skew startup/scaleup; Workday-class employers are the
overwhelming majority of this project's own direct-ATS miss rate --
confirmed against the real Company table, not assumed) -- see
board_discovery.py's docstring for why a from-scratch Workday direct
probe isn't viable (tenant/shard/site can't be guessed from a company
name the way Greenhouse/Lever/Ashby's slugs can).

Unlike Adzuna, this is a single call per cycle regardless of keyword
count: the search API accepts every active keyword as one
`job_title_or` array filter, so intake_service exempts this source from
per-keyword call rotation (see _PER_KEYWORD_CALL_SOURCES). Billing is
per job actually returned ("1 credit = 1 job"), not per call -- see
intake_service._quota_cost -- so the free tier (1,000 jobs/month) is
paced the same way Adzuna's call budget is.

JobsPipe's documented search filters don't include a location/country
parameter, so (like the direct-ATS sources) results are filtered
locally via LocationExclusion instead of a request-time location param.
"""

import os
from datetime import datetime

import requests

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, location_allowed

SOURCE_NAME = "jobspipe"

_BASE_URL = "https://api.jobspipe.dev/v1"
_TIMEOUT = 15


def is_configured() -> bool:
    return bool(os.getenv("JOBSPIPE_API_KEY"))


def _parse_posted_at(posted_at: str) -> datetime | None:
    if not posted_at:
        return None
    try:
        return datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
    except ValueError:
        return None


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    api_key = os.getenv("JOBSPIPE_API_KEY")
    if not keywords:
        return []

    location_exclusions = get_active_location_exclusions()
    try:
        resp = requests.post(
            f"{_BASE_URL}/jobs/search",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={"job_title_or": keywords, "limit": limit},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        results = resp.json().get("data", [])
    except Exception:
        return []

    postings: list[RawPosting] = []
    for job in results:
        # Field naming has been inconsistent between JobsPipe's own docs
        # and what the API actually returns (confirmed live against
        # their sandbox endpoint) -- check multiple candidate names
        # defensively rather than trusting a single one, so a doc/reality
        # drift silently drops fewer postings instead of all of them.
        job_url = job.get("url") or job.get("final_url") or job.get("source_url") or ""
        title = job.get("job_title") or job.get("title") or ""
        if not job_url or not title:
            continue
        job_location = job.get("location")
        if not location_allowed(job_location, location_exclusions):
            continue
        postings.append(
            RawPosting(
                source=SOURCE_NAME,
                external_id=str(job.get("id")) if job.get("id") else None,
                company_name_raw=job.get("company") or "Unknown Company",
                job_title=title,
                job_url=job_url,
                job_description=job.get("description") or None,
                posted_at=_parse_posted_at(job.get("date_posted") or job.get("posted_at") or ""),
                location=job_location,
            )
        )

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan's search response already carries the full description;
    # nothing further to fetch.
    return posting.job_description or ""
