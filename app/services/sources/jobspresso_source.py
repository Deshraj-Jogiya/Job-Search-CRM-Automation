"""
Jobspresso (jobspresso.co) -- public RSS only, confirmed live
2026-09-10. The plain /feed/ URL is the general site blog feed (no
jobs); the real job feed is a WordPress custom post-type feed at
/?feed=job_feed, which carries every current listing (no per-category
split like WeWorkRemotely) plus a custom `job_listing:` XML namespace
with company/location fields as their own elements, not bundled into
the title the way WWR does it.

The feed's own <description> element is a short excerpt (a few
hundred characters); the real full job description lives in the
standard WordPress <content:encoded> field instead, which
feedparser exposes as entry.content[0].value.

No server-side search -- fetches the whole feed each call and filters
locally by title/seniority, same as RemoteOK/WeWorkRemotely.
"""

import html
import re
from datetime import datetime

import feedparser
import requests

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "jobspresso"

_TIMEOUT = 10
_FEED_URL = "https://jobspresso.co/?feed=job_feed"
_HEADERS = {"User-Agent": "Mozilla/5.0"}


def is_configured() -> bool:
    return True  # public RSS, no credentials needed


def _clean_html(raw_html: str) -> str:
    text = re.sub(r"<.*?>", " ", raw_html or "")
    return html.unescape(text)


def _full_description(entry) -> str:
    content = entry.get("content")
    if content:
        return content[0].get("value", "")
    return entry.get("summary", "")


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
        title = entry.get("title", "")
        if not title or not posting_matches(title, keywords, exclusions):
            continue
        job_location = entry.get("job_listing_location") or None
        if not location_allowed(job_location, location_exclusions):
            continue
        job_url = entry.get("link", "")
        postings.append(
            RawPosting(
                source=SOURCE_NAME,
                external_id=entry.get("id") or job_url or None,
                company_name_raw=entry.get("job_listing_company") or "Unknown Company",
                job_title=title,
                job_url=job_url,
                job_description=_clean_html(_full_description(entry)) or None,
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
