"""
Company discovery from Y Combinator's own public company-directory API
(api.ycombinator.com/v0.1/companies) -- a free, official, unauthenticated,
paginated JSON API covering YC's full portfolio (~6,200 companies across
every batch since W05). Filtered to isHiring=true, which the API itself
supports as a query param (confirmed live 2026-08-28: ~1,250 companies) --
a company not currently marked as hiring is unlikely to have a useful
board to probe right now.

Unlike ats_dataset_discovery.py, this source only has company names --
no ATS slug is exposed anywhere in the response -- so a company seeded
from here goes through the same guess-a-slug-from-the-name path as
jobright_discovery.py (board_discovery.discover_slugs via the existing
_backfill_board_slugs sweep), not a pre-verified slug.

Note: workatastartup.com (YC's actual JOB BOARD, as opposed to its
public company directory) requires a logged-in candidate account to
browse -- confirmed live (a bare fetch gets a 406, and following
redirects with a real browser User-Agent still doesn't reach a company
list). This module deliberately does not attempt to work around that;
YC's own public directory API needs no auth at all and gives everything
this app actually needs -- real company names to feed into the existing
slug-discovery pipeline.
"""

import requests

_TIMEOUT = 20
_BASE_URL = "https://api.ycombinator.com/v0.1/companies"
# ~1,250 companies at the API's own ~20-25/page confirmed live -- caps
# well above that so a real cadence change on YC's side (more pages)
# doesn't silently truncate, while still bounding a worst-case runaway.
_MAX_PAGES = 80


def fetch_hiring_company_names() -> list[str]:
    """Returns real company names for YC portfolio companies currently
    marked as hiring. Returns whatever was collected so far if a later
    page fails, rather than raising or discarding earlier pages --
    discovery convenience, not a required source, same fail-soft
    posture as jobright_discovery."""
    names = []
    try:
        url = _BASE_URL
        params = {"isHiring": "true"}
        for _ in range(_MAX_PAGES):
            resp = requests.get(url, params=params, timeout=_TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            names.extend(parse_company_names(data))
            next_page = data.get("nextPage")
            if not next_page:
                break
            url = next_page
            params = None  # nextPage is already a fully-formed URL with its own query string
    except Exception:
        pass
    return names


def parse_company_names(page_data: dict) -> list[str]:
    """The pure parsing half, split out so it's testable against a
    real captured page without a network call."""
    companies = page_data.get("companies") or []
    return [c["name"].strip() for c in companies if isinstance(c, dict) and (c.get("name") or "").strip()]
