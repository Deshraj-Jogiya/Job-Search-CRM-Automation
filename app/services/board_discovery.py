"""
Phase 2 slice 2: auto-detects a company's Greenhouse/Lever/Ashby board
slug from its name, so direct-ATS intake needs zero manual setup for
companies already seen via LinkedIn/Adzuna (see CLAUDE.md/ARCHITECTURE.md
-- "most naturally: auto-detect board slugs for companies already seen").

Board slugs are the URL-friendly identifier each ATS uses for a company's
public job board (e.g. boards.greenhouse.io/stripe -> "stripe"). There's
no search API for "does this company use Greenhouse" -- the only way to
find out is to guess a couple of plausible slugs from the company name
and probe the public listing endpoint directly. A 200 with a parseable
job list means the guess was right; anything else (404, timeout, bad
JSON) means "no board here," which is the overwhelmingly common case
since most companies don't use any of these three ATS platforms, or use
one under a slug that doesn't match this heuristic -- that's fine, this
is a best-effort convenience, not a claim of completeness. Users can
still set/correct a slug manually from the Jobs page.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import requests

from .company_utils import normalize_company_name

_TIMEOUT = 5


def _slug_candidates(company_name: str) -> list[str]:
    normalized = normalize_company_name(company_name)  # lowercased, suffixes stripped, spaces collapsed
    if not normalized:
        return []
    no_space = normalized.replace(" ", "")
    hyphenated = re.sub(r"\s+", "-", normalized)
    candidates = [no_space]
    if hyphenated != no_space:
        candidates.append(hyphenated)
    return candidates


def _probe_greenhouse(slug: str) -> bool:
    try:
        resp = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=_TIMEOUT)
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data.get("jobs"), list)
    except Exception:
        return False


def _probe_lever(slug: str) -> bool:
    try:
        resp = requests.get(f"https://api.lever.co/v1/postings/{slug}?mode=json", timeout=_TIMEOUT)
        if resp.status_code != 200:
            return False
        return isinstance(resp.json(), list)
    except Exception:
        return False


def _probe_ashby(slug: str) -> bool:
    try:
        resp = requests.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=_TIMEOUT)
        if resp.status_code != 200:
            return False
        data = resp.json()
        return isinstance(data.get("jobs"), list)
    except Exception:
        return False


_PROBES = {"greenhouse": _probe_greenhouse, "lever": _probe_lever, "ashby": _probe_ashby}


def discover_slugs(company_name: str) -> dict:
    """Best-effort probe across a couple of slug candidates per ATS.
    Returns {"greenhouse": slug_or_None, "lever": slug_or_None,
    "ashby": slug_or_None}. Stops at the first candidate that hits for
    each ATS -- doesn't try to disambiguate multiple valid-looking hits,
    since that would need a real search API this doesn't have.

    The 3 ATS probes for a given candidate slug are independent network
    calls, so they run concurrently (worst case ~1 timeout instead of
    ~3 stacked) -- this function is called synchronously from a few
    call sites (manual entry, capped backfill batch), so keeping a
    single call fast matters more than keeping this module dependency-
    free."""
    candidates = _slug_candidates(company_name)
    result = {"greenhouse": None, "lever": None, "ashby": None}

    for slug in candidates:
        pending = {ats: probe for ats, probe in _PROBES.items() if result[ats] is None}
        if not pending:
            break
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            futures = {ats: pool.submit(probe, slug) for ats, probe in pending.items()}
            for ats, future in futures.items():
                if future.result():
                    result[ats] = slug

    return result
