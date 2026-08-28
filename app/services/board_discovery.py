"""
Auto-detects a company's Greenhouse/Lever/Ashby/Recruitee/Personio board
slug from its name, so direct-ATS search needs zero manual setup for
companies already seen via LinkedIn/Adzuna.

Board slugs are the URL-friendly identifier each ATS uses for a company's
public job board (e.g. boards.greenhouse.io/stripe -> "stripe"). There's
no search API for "does this company use Greenhouse" -- the only way to
find out is to guess a couple of plausible slugs from the company name
and probe the public listing endpoint directly. A 200 with a parseable
job list means the guess was right; anything else (404, timeout, bad
JSON) means "no board here," which is the overwhelmingly common case
since most companies don't use any of these five ATS platforms, or use
one under a slug that doesn't match this heuristic -- that's fine, this
is a best-effort convenience, not a claim of completeness. Users can
still set/correct a slug manually from the Jobs page.
"""

import re
from concurrent.futures import ThreadPoolExecutor

import requests
from defusedxml import ElementTree

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


def _probe_recruitee(slug: str, company_name: str) -> bool:
    # Recruitee slugs are short and common enough (e.g. "bbc") that an
    # unrelated company can legitimately own the same one -- the API
    # response includes the real employer's name, so cross-check it
    # against the company we're actually looking for instead of
    # trusting "a board exists at this slug" alone. Substring match on
    # the normalized names, since Recruitee's company_name often carries
    # a legal suffix ("BBC NV") the target name won't have.
    try:
        resp = requests.get(f"https://{slug}.recruitee.com/api/offers/", timeout=_TIMEOUT)
        if resp.status_code != 200:
            return False
        data = resp.json()
        if not isinstance(data.get("offers"), list):
            return False
        listed_name = normalize_company_name(data.get("company_name") or "")
        target_name = normalize_company_name(company_name)
        if not listed_name or not target_name:
            return False
        return listed_name in target_name or target_name in listed_name
    except Exception:
        return False


def _probe_personio(slug: str) -> bool:
    # Personio splits customers across .com and .de with no way to tell
    # which from the slug alone -- try both, same as personio_source.py
    # does at fetch time (only the bare slug is persisted, see
    # Company.personio_slug).
    #
    # Unlike Recruitee, Personio's public XML feed carries no
    # company-identifying field at all, so a short/generic slug (e.g.
    # "the-alliance") can silently match an unrelated tenant with no way
    # to catch it mechanically here -- confirmed live (see board_discovery
    # false-positive investigation, 2026-08-24). Users can correct a wrong
    # slug manually from the Jobs page; treat any personio auto-match as
    # lower-confidence than the other four ATS probes.
    for tld in ("com", "de"):
        try:
            resp = requests.get(f"https://{slug}.jobs.personio.{tld}/xml", timeout=_TIMEOUT)
            if resp.status_code != 200:
                continue
            root = ElementTree.fromstring(resp.content)
            if root.tag == "workzag-jobs":
                return True
        except Exception:
            continue
    return False


_PROBES = {
    "greenhouse": lambda slug, company_name: _probe_greenhouse(slug),
    "lever": lambda slug, company_name: _probe_lever(slug),
    "ashby": lambda slug, company_name: _probe_ashby(slug),
    "recruitee": _probe_recruitee,
    "personio": lambda slug, company_name: _probe_personio(slug),
}


def probe_known_slug(ats_type: str, slug: str, company_name: str = "") -> bool:
    """Verifies an ALREADY-KNOWN slug -- e.g. from an external bulk
    dataset, not guessed from a name -- is still live. Skips the
    guess-multiple-candidates step discover_slugs does, but not
    verification itself: an externally-sourced slug can go stale
    between the dataset's own refresh and this app actually using it
    (company renamed its board, switched ATS, shut down). Recruitee
    needs company_name for its same-slug-different-company cross-check
    (see _probe_recruitee); the other four ignore it."""
    probe = _PROBES.get(ats_type)
    if not probe:
        raise ValueError(f"Unknown ATS type '{ats_type}'.")
    return probe(slug, company_name)


def discover_slugs(company_name: str) -> dict:
    """Best-effort probe across a couple of slug candidates per ATS.
    Returns {"greenhouse": slug_or_None, "lever": slug_or_None,
    "ashby": slug_or_None, "recruitee": slug_or_None, "personio":
    slug_or_None}. Stops at the first candidate that hits for each ATS
    -- doesn't try to disambiguate multiple valid-looking hits beyond
    Recruitee's company-name cross-check, since a real search API would
    be needed to do this properly for the rest.

    The 5 ATS probes for a given candidate slug are independent network
    calls, so they run concurrently (worst case ~1 timeout instead of
    ~5 stacked) -- this function is called synchronously from a few
    call sites (manual entry, capped backfill batch), so keeping a
    single call fast matters more than keeping this module dependency-
    free."""
    candidates = _slug_candidates(company_name)
    result = {"greenhouse": None, "lever": None, "ashby": None, "recruitee": None, "personio": None}

    for slug in candidates:
        pending = {ats: probe for ats, probe in _PROBES.items() if result[ats] is None}
        if not pending:
            break
        with ThreadPoolExecutor(max_workers=len(pending)) as pool:
            futures = {ats: pool.submit(probe, slug, company_name) for ats, probe in pending.items()}
            for ats, future in futures.items():
                if future.result():
                    result[ats] = slug

    return result
