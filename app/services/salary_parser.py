"""
Mechanical (no LLM) salary-range extraction from raw JD text --
narrowly scoped, same posture as sponsorship_signals.py: only fires on
an unambiguous, clearly-marked pattern, never guesses, never a silent
filter. This exists to fill part of the "wage-level fit is company-
level, not per-posting" gap (see FUTURE.md) -- Company.max_wage_level_15xx
reflects an employer's HISTORICAL highest DOL-filed wage level, not
what THIS specific posting actually offers.

Real limitation, stated honestly: most job descriptions don't state a
salary at all (this only extracts one when it genuinely is stated,
returning None otherwise -- never inferred from title/seniority/
company size or any other proxy). US pay-transparency laws (NY, CO,
CA, WA and others) mean a real, growing share of postings DO state an
exact range, which is what makes this worth building despite that
limitation -- see wage_level_service.py for how a parsed range (or a
user's manual entry, which this never overwrites) turns into an actual
OEWS wage-level classification.
"""

import re

# $120,000 - $150,000 | $120,000-$150,000 | $120,000 to $150,000
_RANGE_RE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{5,6})\s*(?:-|to|–|—)\s*\$?\s?(\d{2,3}(?:,\d{3})+|\d{5,6})",
    re.IGNORECASE,
)
# $120k - $150k | $120K-$150K
_RANGE_K_RE = re.compile(
    r"\$\s?(\d{2,3})\s*[kK]\s*(?:-|to|–|—)\s*\$?\s?(\d{2,3})\s*[kK]",
)
# A single annual figure explicitly marked as a yearly rate --
# "$140,000 per year" / "$140,000/year" / "$140,000 annually"
_SINGLE_ANNUAL_RE = re.compile(
    r"\$\s?(\d{2,3}(?:,\d{3})+|\d{5,6})\s*(?:/\s*(?:yr|year)|\s+per\s+year|\s+annually)",
    re.IGNORECASE,
)
_SINGLE_ANNUAL_K_RE = re.compile(
    r"\$\s?(\d{2,3})\s*[kK]\s*(?:/\s*(?:yr|year)|\s+per\s+year|\s+annually)",
    re.IGNORECASE,
)
# An explicit hourly rate -- "$55/hour" / "$55 per hour" / "$55/hr" --
# converted to an annual figure via a standard full-time year (2080
# hours = 40hr/week x 52 weeks), documented, never silently assumed to
# be annual.
_HOURLY_RE = re.compile(
    r"\$\s?(\d{2,3}(?:\.\d{1,2})?)\s*(?:/\s*(?:hr|hour)|\s+per\s+hour)",
    re.IGNORECASE,
)

_HOURS_PER_YEAR = 2080


def parse_salary_range(jd_text: str) -> tuple[int, int] | None:
    """Returns (min, max) as whole-dollar annual figures, or None if no
    unambiguous salary is stated. Tries an explicit range first (most
    specific and most common in a pay-transparency posting), then a
    single annual figure, then an hourly rate converted to annual --
    the first pattern that matches wins, nothing is combined or
    averaged across multiple matches."""
    if not jd_text:
        return None

    match = _RANGE_RE.search(jd_text)
    if match:
        low = int(match.group(1).replace(",", ""))
        high = int(match.group(2).replace(",", ""))
        return (low, high) if low <= high else (high, low)

    match = _RANGE_K_RE.search(jd_text)
    if match:
        low, high = int(match.group(1)) * 1000, int(match.group(2)) * 1000
        return (low, high) if low <= high else (high, low)

    match = _SINGLE_ANNUAL_RE.search(jd_text)
    if match:
        value = int(match.group(1).replace(",", ""))
        return value, value

    match = _SINGLE_ANNUAL_K_RE.search(jd_text)
    if match:
        value = int(match.group(1)) * 1000
        return value, value

    match = _HOURLY_RE.search(jd_text)
    if match:
        annual = round(float(match.group(1)) * _HOURS_PER_YEAR)
        return annual, annual

    return None
