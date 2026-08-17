"""
Shared company-name normalization, used both for fuzzy dedup during job
intake and for matching postings to existing Company rows. Kept as one
small utility so every source/consumer normalizes the same way -- a
posting from LinkedIn and one from Adzuna for the same employer need to
collapse to the same Company row.
"""

import re

_SUFFIX_RE = re.compile(
    r"\b(inc|llc|ltd|co|corp|corporation|company|solutions|technologies|"
    r"technology|group|holdings|international|intl)\b\.?",
    re.IGNORECASE,
)
_PUNCT_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_company_name(raw_name: str) -> str:
    """Collapse a raw company name to a comparable key: lowercased,
    common corporate suffixes stripped, punctuation removed, whitespace
    collapsed. Not meant to be displayed -- only for equality/fuzzy
    matching."""
    if not raw_name:
        return ""
    name = _SUFFIX_RE.sub("", raw_name)
    name = _PUNCT_RE.sub(" ", name)
    name = _WHITESPACE_RE.sub(" ", name)
    return name.strip().lower()


def normalize_title(raw_title: str) -> str:
    """Same idea for job titles -- lowercased and whitespace-collapsed
    so 'Senior  Data Engineer' and 'senior data engineer' match."""
    if not raw_title:
        return ""
    return _WHITESPACE_RE.sub(" ", raw_title).strip().lower()
