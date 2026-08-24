"""
Direct Recruitee job board intake -- same shape as
greenhouse_source.py/lever_source.py/ashby_source.py, driven by Company
rows with a recruitee_slug set. Recruitee's public careers-site API
(https://{slug}.recruitee.com/api/offers/) is the same unauthenticated,
per-company endpoint the platform's own careers-page widget calls --
distinct from the documented but authenticated /c/{company_id}/offers
API. Unlike Greenhouse/Ashby, the listing response doesn't include the
full description (only a short "highlight" blurb), so
fetch_full_description() does a second per-posting call to
/api/offers/{slug}, same as linkedin_source.py.
"""

import html
import re

import requests
from sqlalchemy.orm import Session

from ...database import SessionLocal
from ...models import Company
from .base import RawPosting
from .keyword_matching import get_active_location_exclusions, get_active_seniority_exclusions, location_allowed, posting_matches

SOURCE_NAME = "recruitee"

_TIMEOUT = 10


def _active_target_companies(db: Session) -> list[Company]:
    return (
        db.query(Company)
        .filter(Company.recruitee_slug.isnot(None), Company.status != "Blocked")
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
                f"https://{company.recruitee_slug}.recruitee.com/api/offers/",
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            offers = resp.json().get("offers", [])
        except Exception:
            continue

        for offer in offers:
            title = offer.get("title", "")
            if not title or not posting_matches(title, keywords, exclusions):
                continue
            offer_location = offer.get("location") or offer.get("city")
            if not location_allowed(offer_location, location_exclusions):
                continue
            postings.append(
                RawPosting(
                    source=SOURCE_NAME,
                    external_id=str(offer.get("id")) if offer.get("id") else None,
                    company_name_raw=company.name,
                    job_title=title,
                    job_url=offer.get("careers_url", ""),
                    job_description=_clean_html(offer.get("highlight", "")) or None,
                    location=offer_location,
                )
            )
            if len(postings) >= limit * max(len(companies), 1):
                break

    return postings


def fetch_full_description(posting: RawPosting) -> str:
    try:
        slug = posting.job_url.rstrip("/").rsplit("/", 1)[-1]
        subdomain = posting.job_url.split("//", 1)[-1].split(".recruitee.com", 1)[0]
        resp = requests.get(
            f"https://{subdomain}.recruitee.com/api/offers/{slug}",
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        offer = resp.json().get("offer", {})
    except Exception:
        return posting.job_description or ""

    parts = [offer.get("description", ""), offer.get("requirements", "")]
    text = _clean_html(" ".join(p for p in parts if p))
    return text or posting.job_description or ""
