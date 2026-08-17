"""
Common shape every intake source returns, and the light convention each
source module follows. Not a strict ABC -- sources genuinely differ in
how cheap a full description is to obtain (Adzuna's search response
usually already includes one; LinkedIn's guest search does not and
needs a second per-posting fetch), so intake_service checks
`job_description` and only calls `fetch_full_description()` when it's
still missing, and only for postings that survive dedup.

Each source module (linkedin_source.py, adzuna_source.py, ...) exposes:

    SOURCE_NAME: str

    def cheap_scan(keywords: list[str], location: str, limit: int) -> list[RawPosting]
        Cheapest possible listing fetch -- title/company/url/posted_at,
        `job_description` populated only if the source's own listing
        response already includes it for free.

    def fetch_full_description(posting: RawPosting) -> str
        Fetch the full JD text for a posting that survived dedup.
        Only called when posting.job_description is still None.

    def is_configured() -> bool
        Whether this source has what it needs to run at all (e.g. an
        API key). Sources that aren't configured are skipped by
        intake_service with a clear activity log entry, not an error.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class RawPosting:
    source: str
    company_name_raw: str
    job_title: str
    job_url: str
    external_id: str | None = None
    job_description: str | None = None
    posted_at: datetime | None = None
