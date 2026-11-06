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
from datetime import datetime, timezone


def as_naive_utc(value: datetime) -> datetime:
    """Stored datetimes are naive UTC; some sources parse aware ones."""
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def parse_posted_at(value) -> datetime | None:
    """A board's own publish date as naive UTC, matching utcnow(). Accepts the
    shapes the ATS APIs return: ISO 8601 (with Z or an offset), epoch
    milliseconds (Lever), and "YYYY-MM-DD HH:MM:SS UTC" (Recruitee). Returns
    None for anything else, so a bad date never drops a posting."""
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).replace(tzinfo=None)
        text = str(value).strip().replace(" UTC", "+00:00").replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except (ValueError, OverflowError, OSError):
        return None
    return as_naive_utc(parsed)


@dataclass
class RawPosting:
    source: str
    company_name_raw: str
    job_title: str
    job_url: str
    external_id: str | None = None
    job_description: str | None = None
    posted_at: datetime | None = None
    location: str | None = None
