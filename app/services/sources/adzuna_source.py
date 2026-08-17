"""
Adzuna job search API -- free tier (~1,000 calls/month per CLAUDE.md's
noted budget), requires ADZUNA_APP_ID / ADZUNA_APP_KEY. Unlike LinkedIn,
every search call here counts against a real quota, so intake_service
is responsible for polling this source less often (see JobSource /
GlobalSettings), not this module. If credentials aren't configured,
is_configured() returns False and intake_service skips this source
entirely with a clear log entry rather than erroring.

Adzuna's search response already includes a description snippet (often
the full text) for free, so cheap_scan() populates job_description
directly -- fetch_full_description() is a no-op fallback only used if
that snippet was empty.
"""

import os
from datetime import datetime

import requests

from .base import RawPosting

SOURCE_NAME = "adzuna"


def is_configured() -> bool:
    return bool(os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY"))


def _parse_posted_at(created: str) -> datetime | None:
    if not created:
        return None
    try:
        return datetime.strptime(created[:10], "%Y-%m-%d")
    except ValueError:
        return None


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    country = os.getenv("ADZUNA_COUNTRY", "us")

    postings: list[RawPosting] = []
    for keyword in keywords:
        url = f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
        params = {
            "app_id": app_id,
            "app_key": app_key,
            "results_per_page": limit,
            "what": keyword,
            "where": location,
            "content-type": "application/json",
        }
        try:
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()
        except Exception:
            continue

        for result in data.get("results", []):
            job_url = result.get("redirect_url", "")
            if not job_url:
                continue
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=str(result.get("id", "")) or None,
                    company_name_raw=(result.get("company") or {}).get("display_name", "Unknown Company"),
                    job_title=result.get("title", ""),
                    job_url=job_url,
                    job_description=result.get("description") or None,
                    posted_at=_parse_posted_at(result.get("created", "")),
                )
            )

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # Adzuna's search response already carries the description; nothing
    # further to fetch. If it was empty, there's no separate detail
    # endpoint on the free tier worth spending another call on.
    return posting.job_description or ""
