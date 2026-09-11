"""
Auto-detects a company's Greenhouse/Lever/Ashby/Recruitee/Personio/
Workable/SmartRecruiters board slug from its name, so direct-ATS search
needs zero manual setup for companies already seen via LinkedIn/Adzuna.

Board slugs are the URL-friendly identifier each ATS uses for a company's
public job board (e.g. boards.greenhouse.io/stripe -> "stripe"). There's
no search API for "does this company use Greenhouse" -- the only way to
find out is to guess a couple of plausible slugs from the company name
and probe the public listing endpoint directly. A 200 with a parseable
job list means the guess was right; anything else (404, timeout, bad
JSON) means "no board here," which is the overwhelmingly common case
since most companies don't use any of these seven ATS platforms, or use
one under a slug that doesn't match this heuristic -- that's fine, this
is a best-effort convenience, not a claim of completeness. Users can
still set/correct a slug manually from the Jobs page.

Workable and SmartRecruiters (added 2026-09-10, Phase 2a of the H-1B
Targeting Upgrade) both need a real cross-check beyond "got a 200,"
same spirit as Recruitee's og:site_name check above but for different
reasons each:
  - Workable's widget API 404s cleanly for a slug nobody owns, but a
    slug that IS owned can belong to a same-named unrelated business
    (confirmed live: the "notion" slug is a small UK events agency
    called Notion, not the productivity company) -- cross-check the
    response's own `name` field.
  - SmartRecruiters' Postings API returns 200 + `totalFound: 0` for
    ANY identifier, real or completely made up (confirmed live against
    a nonsense identifier) -- a 200 alone proves nothing. Only a
    nonzero `totalFound` plus a `company.name` cross-check counts as a
    match, which means a real SmartRecruiters employer with zero
    currently-open postings won't be auto-detected -- an accepted
    best-effort limitation, same class as Personio's below.
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
    # The v1 endpoint (.../v1/postings/{slug}?mode=json) started
    # returning 401 Unauthorized for every company tested (2026-08-28,
    # confirmed against real live customers pulled fresh from the
    # ats-scrapers dataset, and against flagship names like Netflix/
    # Notion/Plaid -- not a per-company issue, and not a curl-specific
    # block, checked with a real browser User-Agent too). The older v0
    # path is still public and returns the same posting shape (same
    # field names lever_source.py already parses), so use that instead.
    try:
        resp = requests.get(f"https://api.lever.co/v0/postings/{slug}", timeout=_TIMEOUT)
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


_OG_SITE_NAME_RE = re.compile(
    r'property="og:site_name"[^>]*content="([^"]*)"|content="([^"]*)"[^>]*property="og:site_name"'
)


def _probe_recruitee(slug: str, company_name: str) -> bool:
    # Recruitee slugs are short and common enough (e.g. "bbc") that an
    # unrelated company can legitimately own the same one -- originally
    # cross-checked via the API's own `company_name` field, but Recruitee
    # removed that field from /api/offers/ (confirmed 2026-08-28: the
    # response is bare {"offers": [...]} now, nothing else). The public
    # careers page itself still carries the real name via its
    # `og:site_name` meta tag, so fetch that instead -- `requests`
    # follows redirects by default, which turns out to matter: a slug
    # whose careers page has moved to a custom domain (e.g. "bbc" now
    # redirects to careers.bbc.be) lands on that domain's own
    # og:site_name, confirmed live to read "BBC NV" -- the exact same
    # real company this check exists to reject, not the British
    # Broadcasting Corporation. A slug with no hosted page at all
    # (redirects to Recruitee's generic marketing site) has no
    # og:site_name to find, so it's correctly rejected too rather than
    # trusted on "offers exist" alone.
    try:
        offers_resp = requests.get(f"https://{slug}.recruitee.com/api/offers/", timeout=_TIMEOUT)
        if offers_resp.status_code != 200:
            return False
        offers_data = offers_resp.json()
        if not isinstance(offers_data.get("offers"), list):
            return False

        page_resp = requests.get(f"https://{slug}.recruitee.com/", timeout=_TIMEOUT)
        if page_resp.status_code != 200:
            return False
        match = _OG_SITE_NAME_RE.search(page_resp.text)
        if not match:
            return False
        listed_name = normalize_company_name(match.group(1) or match.group(2) or "")
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


def _names_cross_check(listed_name: str, company_name: str) -> bool:
    """Same loose containment check _probe_recruitee uses -- exact
    equality is too strict (display names carry legal suffixes,
    punctuation, etc. that normalize_company_name doesn't always fully
    collapse), pure substring is too loose on its own, but on already-
    normalized short strings containment in either direction is a
    reasonable middle ground for this best-effort probe."""
    listed = normalize_company_name(listed_name)
    target = normalize_company_name(company_name)
    if not listed or not target:
        return False
    return listed in target or target in listed


def _probe_workable(slug: str, company_name: str) -> bool:
    try:
        resp = requests.get(
            f"https://apply.workable.com/api/v1/widget/accounts/{slug}",
            params={"details": "true"},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        if not isinstance(data.get("jobs"), list):
            return False
        return _names_cross_check(data.get("name") or "", company_name)
    except Exception:
        return False


def _probe_smartrecruiters(slug: str, company_name: str) -> bool:
    try:
        resp = requests.get(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
            params={"limit": 1},
            timeout=_TIMEOUT,
        )
        if resp.status_code != 200:
            return False
        data = resp.json()
        content = data.get("content")
        # totalFound is the only real signal here -- see module
        # docstring, this endpoint returns 200 for any identifier.
        if not data.get("totalFound") or not isinstance(content, list) or not content:
            return False
        listed_name = (content[0].get("company") or {}).get("name") or ""
        return _names_cross_check(listed_name, company_name)
    except Exception:
        return False


_PROBES = {
    "greenhouse": lambda slug, company_name: _probe_greenhouse(slug),
    "lever": lambda slug, company_name: _probe_lever(slug),
    "ashby": lambda slug, company_name: _probe_ashby(slug),
    "recruitee": _probe_recruitee,
    "personio": lambda slug, company_name: _probe_personio(slug),
    "workable": _probe_workable,
    "smartrecruiters": _probe_smartrecruiters,
}


def fetch_verified_name(ats_type: str, slug: str) -> str | None:
    """When the ATS's own public API response carries a real employer
    name, returns it -- used by job_board_aggregator_discovery's
    callers to replace a slug-derived guess (e.g. "1456754456yhgbhfg")
    with the real company name (e.g. "Davidson Kempner Capital
    Management") whenever the platform actually exposes one, instead
    of leaving an obviously-provisional name in place. Confirmed live
    (2026-08-27): Greenhouse's job-list API includes a real
    `company_name` field per job. Ashby's does not -- no company/
    organization field exists anywhere in its response, confirmed by
    inspecting the full real job object -- so this returns None for
    Ashby rather than guessing from description text (a wrong guessed
    name would be worse than an honest slug-derived placeholder). Lever
    also returns None -- its postings (see _probe_lever) carry no
    company-identifying field either, confirmed on the same real job
    object."""
    try:
        if ats_type == "greenhouse":
            resp = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=_TIMEOUT)
            if resp.status_code != 200:
                return None
            for job in resp.json().get("jobs") or []:
                name = (job.get("company_name") or "").strip()
                if name:
                    return name
            return None
        return None
    except Exception:
        return None


def probe_known_slug(ats_type: str, slug: str, company_name: str = "") -> bool:
    """Verifies an ALREADY-KNOWN slug -- e.g. from an external bulk
    dataset, not guessed from a name -- is still live. Skips the
    guess-multiple-candidates step discover_slugs does, but not
    verification itself: an externally-sourced slug can go stale
    between the dataset's own refresh and this app actually using it
    (company renamed its board, switched ATS, shut down). Recruitee,
    Workable, and SmartRecruiters need company_name for their same-slug
    -different-company cross-checks; the other four ignore it."""
    probe = _PROBES.get(ats_type)
    if not probe:
        raise ValueError(f"Unknown ATS type '{ats_type}'.")
    return probe(slug, company_name)


def discover_slugs(company_name: str) -> dict:
    """Best-effort probe across a couple of slug candidates per ATS.
    Returns one {ats_name: slug_or_None} entry per platform in _PROBES
    (currently greenhouse/lever/ashby/recruitee/personio/workable/
    smartrecruiters). Stops at the first candidate that hits for each
    ATS -- doesn't try to disambiguate multiple valid-looking hits
    beyond each platform's own company-name cross-check where it has
    one, since a real search API would be needed to do this properly
    for the rest.

    The ATS probes for a given candidate slug are independent network
    calls, so they run concurrently (worst case ~1 timeout instead of
    N stacked) -- this function is called synchronously from a few
    call sites (manual entry, capped backfill batch), so keeping a
    single call fast matters more than keeping this module dependency-
    free."""
    candidates = _slug_candidates(company_name)
    result = {ats: None for ats in _PROBES}

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
