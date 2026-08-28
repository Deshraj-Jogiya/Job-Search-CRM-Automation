"""
Secondary company-slug discovery from the job-board-aggregator open
dataset (github.com/Feashliaa/job-board-aggregator), a free,
daily-refreshed list of real ATS board slugs. Unlike
ats_dataset_discovery.py's source, this one only publishes bare slugs
-- no company display name alongside them, and no recruitee/personio
coverage at all (verified 2026-08-27: only greenhouse, lever, and
ashby files exist in its data/ directory; recruitee/personio return
404). It's used purely as a secondary supplement for the 3 platforms
it does cover: candidates this surfaces that ats_dataset_discovery.py's
source doesn't already have.

The slug itself becomes a provisional company name (title-cased) until
board_discovery's live probe confirms the board is real. Also verified
directly: a small number of the raw slugs are non-name artifacts (bare
numeric IDs, garbled strings like "1456754456yhgbhfg") rather than real
company slugs -- filtered out here since "is this a plausible company
slug" is this module's own data-quality concern, not board_discovery's.
Same lower-confidence-until-verified posture board_discovery.py already
applies to Personio.
"""

import requests

_TIMEOUT = 20
_BASE_URL = "https://raw.githubusercontent.com/Feashliaa/job-board-aggregator/main/data/{ats}_companies.json"

# Confirmed 2026-08-27: recruitee/personio have no file in this dataset.
_SUPPORTED_ATS = ("greenhouse", "lever", "ashby")


def _looks_like_a_real_slug(slug: str) -> bool:
    return bool(slug) and not slug.isdigit() and len(slug) >= 2


def fetch_slugs_by_ats() -> dict[str, list[str]]:
    """Returns {"greenhouse": [slug, ...], "lever": [...], "ashby":
    [...]}. Each platform is fetched independently -- one platform's
    file being missing/broken shouldn't take down the other two, so
    failures are swallowed per-platform rather than for the whole
    call."""
    result = {}
    for ats in _SUPPORTED_ATS:
        try:
            resp = requests.get(_BASE_URL.format(ats=ats), timeout=_TIMEOUT)
            resp.raise_for_status()
            result[ats] = parse_slug_list(resp.json())
        except Exception:
            continue
    return result


def parse_slug_list(raw) -> list[str]:
    """The pure parsing half, split out so it's testable without a
    network call."""
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, str) and _looks_like_a_real_slug(s)]


def slug_to_provisional_name(slug: str) -> str:
    """A readable placeholder company name derived from the slug alone
    -- e.g. "acme-labs" -> "Acme Labs". Explicitly provisional: this is
    not a real company name, just something more presentable than a raw
    slug until a human corrects it from the Jobs page."""
    return slug.replace("-", " ").replace("_", " ").title()
