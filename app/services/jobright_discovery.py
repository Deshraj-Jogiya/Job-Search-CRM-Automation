"""
Company discovery from JobRight's free public job-list repo. This is
deliberately NOT a `sources/*` intake module -- JobRight's own listing
has no real per-posting JD text and its "apply" links route through
jobright.ai rather than the employer's own ATS page, so treating it as
a raw posting feed is a dead end (confirmed directly: the table's only
columns are company/title/level/location/H1B-status/link/date).

What IS genuinely useful: the company names it surfaces are a real,
daily-updated signal of who is actively hiring in tech, independent of
whatever LinkedIn/Adzuna's own search happens to have indexed. This
module only extracts company names for titles that match the existing
active search keywords -- the actual ingestion of real postings (with
real JD text and a real ATS apply URL) still happens entirely through
the existing Greenhouse/Lever/Ashby direct-board pipeline, once
board_discovery's existing slug-probing sweep picks up a newly-seeded
company. Same "pure fetch, no DB access" shape as board_discovery.py --
the caller (intake_service._discover_companies_from_jobright) owns the
actual Company row creation.
"""

import re

import requests

_TIMEOUT = 15

# The org (jobright-ai) publishes ~36 repos, but nearly all of them are
# scoped to new-grad/internship-only listings. This is the one general-
# audience repo (any seniority level, not new-grad-gated) -- verified
# directly by inspecting its real table content, which includes Junior
# through Staff/Principal/Lead rows side by side.
_README_URL = "https://raw.githubusercontent.com/jobright-ai/Daily-H1B-Jobs-In-Tech/master/README.md"

_COMPANY_LINK_RE = re.compile(r"\*\*\[([^\]]+)\]\([^)]*\)\*\*")


def fetch_matching_companies(active_keywords: list[str]) -> list[str]:
    """Returns a deduped list of real company names from the JobRight
    table whose job title matches at least one active search keyword
    (the same matching used by the direct-board sources, via
    keyword_matching.matches_keywords) -- not every company in the
    table, just ones with a title genuinely relevant to what this
    candidate is actually searching for. Returns [] on any fetch/parse
    failure rather than raising -- this is a discovery convenience, not
    a required source, same fail-soft posture as board_discovery."""
    if not active_keywords:
        return []
    try:
        resp = requests.get(_README_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        text = resp.text
    except Exception:
        return []
    return parse_matching_companies(text, active_keywords)


def parse_matching_companies(text: str, active_keywords: list[str]) -> list[str]:
    """The pure parsing half of fetch_matching_companies, split out so
    it's testable against a real captured table snippet without a
    network call."""
    from .sources.keyword_matching import matches_keywords

    if not active_keywords:
        return []

    companies: list[str] = []
    seen = set()
    current_company = None

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 2:
            continue

        first_cell, title_cell = cells[0], cells[1]

        # Skip header/separator rows (e.g. "| Company | Job Title | ..." or
        # "| ------- | --------- | ...") -- neither names a real company.
        if first_cell.lower() == "company" or set(first_cell) <= {"-", " "}:
            continue

        match = _COMPANY_LINK_RE.search(first_cell)
        if match:
            current_company = match.group(1).strip()
        elif first_cell != "↳":  # "↳" continuation marker -- same company as the row above
            current_company = None  # an unrecognized first cell -- don't misattribute to a stale company

        if not current_company or not title_cell:
            continue
        if not matches_keywords(title_cell, active_keywords):
            continue
        if current_company not in seen:
            seen.add(current_company)
            companies.append(current_company)

    return companies
