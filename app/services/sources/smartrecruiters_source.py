"""
Direct SmartRecruiters job board intake. Unlike the other direct-ATS
sources, the public Postings API's list response
(api.smartrecruiters.com/v1/companies/{id}/postings) doesn't include
the job description -- only the per-posting detail endpoint
(.../postings/{postingId}) does, inside jobAd.sections.*.text -- so
this is the first direct-ATS source here that needs a real second
fetch (see base.py's docstring on fetch_full_description; the other
direct sources all get full content in one call). The detail endpoint
is reconstructed from the public job URL this module builds in
cheap_scan (jobs.smartrecruiters.com/{identifier}/{id}, confirmed live
to resolve without a title slug suffix) rather than threading the
company identifier through RawPosting separately.

Driven by which Company rows have a smartrecruiters_slug set (see
board_discovery.py's _probe_smartrecruiters -- note its real
limitation: the list endpoint returns 200 + totalFound: 0 for even a
completely nonexistent company identifier, so auto-detection can only
ever confirm a match when the guessed identifier currently has at
least one open posting; a real employer with zero open roles at probe
time won't be auto-detected, same class of best-effort limitation as
Personio's).
"""

import html
import re

import requests
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "smartrecruiters"

_TIMEOUT = 10
_POSTING_URL_RE = re.compile(r"jobs\.smartrecruiters\.com/([^/]+)/(\d+)")

# The full description lives across several named sections rather than
# one field -- concatenated in this order (job content first, company
# blurb last) so the most relevant text comes first for downstream
# keyword/scam-pattern matching.
_DESCRIPTION_SECTIONS = ("jobDescription", "qualifications", "additionalInformation", "companyDescription")


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.smartrecruiters_slug.isnot(None), Company.status != "Blocked")
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


def _job_location(job: dict) -> str | None:
    loc = job.get("location") or {}
    return loc.get("fullLocation") or loc.get("city") or None


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
        try:
            resp = requests.get(
                f"https://api.smartrecruiters.com/v1/companies/{company.smartrecruiters_slug}/postings",
                params={"limit": 100},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            jobs = resp.json().get("content", [])
        except Exception:
            continue

        for job in jobs:
            title = job.get("name", "")
            if not title or not posting_matches(title, keywords, exclusions):
                continue
            job_location = _job_location(job)
            if not location_allowed(job_location, location_exclusions):
                continue
            job_id = job.get("id")
            job_url = (
                f"https://jobs.smartrecruiters.com/{company.smartrecruiters_slug}/{job_id}" if job_id else ""
            )
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=job_id,
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=job_url,
                    location=job_location,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    match = _POSTING_URL_RE.search(posting.job_url or "")
    if not match:
        return ""
    identifier, posting_id = match.group(1), match.group(2)
    try:
        resp = requests.get(
            f"https://api.smartrecruiters.com/v1/companies/{identifier}/postings/{posting_id}",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        sections = (resp.json().get("jobAd") or {}).get("sections") or {}
    except Exception:
        return ""

    parts = []
    for key in _DESCRIPTION_SECTIONS:
        text = (sections.get(key) or {}).get("text")
        if text:
            parts.append(_clean_html(text))
    return "\n\n".join(parts)
