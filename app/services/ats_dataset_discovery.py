"""
Company + real ATS-slug discovery from the ats-scrapers open dataset
(github.com/kalil0321/ats-scrapers, published at storage.stapply.ai),
a free, no-auth, daily-refreshed CSV of ~80,000 real companies mapped
to their real ATS board slugs across 65 platforms.

Unlike jobright_discovery.py (company names only -- the slug still has
to be guessed and probed by board_discovery.discover_slugs), this
source already supplies the real slug directly, so a seeded company
here can skip straight to a verified slug on first save. It does NOT
skip verification, though: the dataset can drift (a company renames
its board, moves ATS, or shuts down) between its own daily refresh and
when this app actually uses a row, so every (name, slug) pair is still
re-probed live via board_discovery.probe_known_slug before being
trusted -- same "discover real info, don't guess" posture as every
other source here.

Verified directly (2026-08-27): the single companies.csv (not a
per-ATS file) has columns ats,name,slug,url and covers all 5 platforms
board_discovery.py knows how to probe -- 6031 greenhouse, 2402 lever,
3448 ashby, 1164 recruitee, 2463 personio rows.
"""

import csv
import io

import requests

_TIMEOUT = 30
_COMPANIES_CSV_URL = "https://storage.stapply.ai/jobhive/v1/companies.csv"

# The 5 ATS platforms Career Pilot's own board_discovery.py knows how
# to probe -- the dataset covers 65 platforms total, but there's no
# point pulling rows this app has no way to act on.
_SUPPORTED_ATS = ("greenhouse", "lever", "ashby", "recruitee", "personio")


def fetch_companies_by_ats() -> dict[str, list[tuple[str, str]]]:
    """Returns {"greenhouse": [(name, slug), ...], "lever": [...], ...}
    for the 5 supported platforms. Returns {} on any fetch/parse
    failure rather than raising -- discovery convenience, not a
    required source, same fail-soft posture as jobright_discovery."""
    try:
        resp = requests.get(_COMPANIES_CSV_URL, timeout=_TIMEOUT)
        resp.raise_for_status()
        return parse_companies_csv(resp.text)
    except Exception:
        return {}


def parse_companies_csv(csv_text: str) -> dict[str, list[tuple[str, str]]]:
    """The pure parsing half, split out so it's testable against a
    real captured CSV snippet without a network call. Expects the
    dataset's own real column names: ats, name, slug, url."""
    reader = csv.DictReader(io.StringIO(csv_text))
    result = {ats: [] for ats in _SUPPORTED_ATS}
    for row in reader:
        ats = (row.get("ats") or "").strip()
        if ats not in result:
            continue
        name = (row.get("name") or "").strip()
        slug = (row.get("slug") or "").strip()
        if name and slug:
            result[ats].append((name, slug))
    return result
