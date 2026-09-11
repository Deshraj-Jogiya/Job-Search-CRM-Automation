"""
We Work Remotely (weworkremotely.com) -- public RSS only (confirmed
live, 2026-09-10: no JSON API exists, feeds are per-category, this
app polls the "remote-programming-jobs" category, the closest fit for
data engineering/ML/analytics roles -- WWR has no more specific
data-focused category). Feed's own <ttl>60</ttl> signals hourly
polling is the intended pace -- shared with the other 3 Phase 2b
sources' remote_board_poll_interval_minutes setting (conservative
relative to this feed's own signal, matched to Remotive's stricter
real constraint instead, see remotive_source.py).

WWR's title format bundles company and role together
("{Company}: {Job Title}") rather than as separate fields -- split on
the first colon; a title with no colon (rare, but not impossible)
falls back to "Unknown Company" rather than guessing.

No server-side search -- fetches the whole category feed each call
and filters locally by title/seniority, same as RemoteOK/Jobspresso.
"""

import html
import re
from datetime import datetime

import feedparser
import requests

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "weworkremotely"

_TIMEOUT = 10
_FEED_URL = "https://weworkremotely.com/categories/remote-programming-jobs.rss"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def is_configured() -> bool:
    return True  # public RSS, no credentials needed


def _clean_html(raw_html: str) -> str:
    text = re.sub(r"<.*?>", " ", raw_html or "")
    return html.unescape(text)


def _split_title(raw_title: str) -> tuple[str, str]:
    """"Aker Systems: Principal Software Engineer" -> ("Aker Systems",
    "Principal Software Engineer"). Falls back to ("Unknown Company",
    raw_title) if there's no colon to split on."""
    if ":" in raw_title:
        company, _, title = raw_title.partition(":")
        return company.strip(), title.strip()
    return "Unknown Company", raw_title.strip()


def _parse_posted_at(entry) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6])
    except (TypeError, ValueError):
        return None


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    exclusions = get_active_seniority_exclusions()
    location_exclusions = get_active_location_exclusions()

    try:
        resp = requests.get(_FEED_URL, headers=_HEADERS, timeout=_TIMEOUT)
        resp.raise_for_status()
        feed = feedparser.parse(resp.content)
    except Exception:
        return []

    postings: list[RawPosting] = []
    for entry in feed.entries:
        company, title = _split_title(entry.get("title", ""))
        if not title or not posting_matches(title, keywords, exclusions):
            continue
        job_location = entry.get("region") or None
        if not location_allowed(job_location, location_exclusions):
            continue
        job_url = entry.get("link", "")
        postings.append(
            RawPosting(
                source=SOURCE_NAME,
                external_id=entry.get("id") or job_url or None,
                company_name_raw=company,
                job_title=title,
                job_url=job_url,
                job_description=_clean_html(entry.get("summary", "")) or None,
                posted_at=_parse_posted_at(entry),
                location=job_location,
            )
        )
        if len(postings) >= limit:
            break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    # cheap_scan already fetches the full description in one call.
    return posting.job_description or ""
