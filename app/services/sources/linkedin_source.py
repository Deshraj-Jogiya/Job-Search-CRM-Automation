"""
LinkedIn public guest job search -- no API key, no login, but a scrape
against an undocumented endpoint (ToS-risky, brittle to markup changes;
this is why CLAUDE.md wants it treated as one source among several, not
the sole strategy). cheap_scan() hits the guest search listing (many
postings per request, no per-posting cost); fetch_full_description()
is the separate, more expensive per-posting page fetch, only called
for postings that survive dedup.
"""

import html
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, location_allowed

SOURCE_NAME = "linkedin"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
    )
}


def is_configured() -> bool:
    return True  # no credentials needed -- always available


def _clean_html(raw_html: str) -> str:
    text = re.sub(r"<.*?>", "", raw_html)
    return html.unescape(text).strip()


def _parse_posted_at(datetime_text: str) -> datetime | None:
    if not datetime_text:
        return None
    try:
        return datetime.strptime(datetime_text.strip(), "%Y-%m-%d")
    except ValueError:
        return None


def cheap_scan(keywords: list[str], location: str, limit: int = 15) -> list[RawPosting]:
    location_exclusions = get_active_location_exclusions()
    postings: list[RawPosting] = []
    for keyword in keywords:
        url = (
            "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
            f"?keywords={urllib.parse.quote(keyword)}&location={urllib.parse.quote(location)}"
            "&f_TPR=r2592000&start=0"
        )
        req = urllib.request.Request(url, headers=_HEADERS)
        try:
            with urllib.request.urlopen(req, timeout=10) as response:
                page = response.read().decode("utf-8", errors="ignore")
        except Exception:
            continue

        count = 0
        for block in page.split("<li")[1:]:
            url_match = re.search(r'<a class="base-card__full-link[^"]*" href="([^"]+)"', block)
            title_match = re.search(r'<span class="sr-only">\s*([^\n<]+)\s*</span>', block)
            company_match = re.search(
                r'<h4 class="base-search-card__subtitle">\s*<a[^>]*>\s*([^\n<]+)\s*</a>', block
            ) or re.search(r'<h4 class="base-search-card__subtitle">\s*([^\n<]+)\s*</h4>', block)
            datetime_match = re.search(r'<time[^>]*datetime="([^"]+)"', block)
            location_match = re.search(
                r'<span class="job-search-card__location">\s*([^\n<]+)\s*</span>', block
            )

            if not (url_match and title_match):
                continue

            job_location = _clean_html(location_match.group(1)) if location_match else None
            if not location_allowed(job_location, location_exclusions):
                continue

            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    company_name_raw=_clean_html(company_match.group(1)) if company_match else "Unknown Company",
                    job_title=_clean_html(title_match.group(1)),
                    job_url=url_match.group(1).split("?")[0],
                    posted_at=_parse_posted_at(datetime_match.group(1)) if datetime_match else None,
                    location=job_location,
                )
            )
            count += 1
            if count >= limit:
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    req = urllib.request.Request(posting.job_url, headers=_HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            page = response.read().decode("utf-8", errors="ignore")
    except Exception:
        return ""

    match = re.search(r'<div class="show-more-less-html__markup[^"]*">(.*?)</div>', page, re.DOTALL)
    if not match:
        match = re.search(r'<div class="description__text[^"]*">(.*?)</div>', page, re.DOTALL)
    return _clean_html(match.group(1)) if match else ""
