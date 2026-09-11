"""
Derives Company.tier (A/B/C/X) from the H-1B/wage signal Phase 1
already collects. Thresholds are config-driven
(GlobalSettings.tier_a_min_filings etc.), never hardcoded here, so the
bar for "proven sponsor" stays a live-editable judgment call rather
than a constant baked into code.

"Filings" for tier purposes combines both sponsorship-activity signals
this app tracks -- Company.lca_filings_total (DOL LCA Disclosure, real
filed intent to sponsor) and Company.h1b_approvals_total (USCIS
Employer Data Hub, real approved petitions) -- rather than either
alone. Neither source is broken down by fiscal year in this schema
(see ingest/sponsors.py -- both loaders store one running cumulative
total, not a per-FY series), so "across FY24-26" from the original
spec is honored by loading only FY24-26 source files through
ingest/cli.py, not by filtering a stored per-year breakdown that
doesn't exist. Documented explicitly here rather than silently
approximated.

Tier rules (thresholds read from GlobalSettings; the numbers below are
just the shipped defaults):
    A: total_filings >= tier_a_min_filings AND wage rank >= tier_a_min_wage_level
    B: total_filings >= tier_b_min_filings
    C: 1-2 filings, OR cap-exempt with zero filings on record
    X: zero filings and not cap-exempt

Cap-exempt employers file year-round outside the annual lottery -- a
cap-exempt company with enough real filing history to qualify for A or
B keeps that tier (cap-exempt never caps a tier down); it only
provides a floor at C when there's no filing history at all to go on.
"""

from ..models import Company, GlobalSettings

# Prevailing-wage level letters -> a plain rank for >= comparison against
# GlobalSettings.tier_a_min_wage_level (stored as an int 1-4).
WAGE_LEVEL_RANK = {"I": 1, "II": 2, "III": 3, "IV": 4}


def _total_filings(company: Company) -> int:
    return (company.lca_filings_total or 0) + (company.h1b_approvals_total or 0)


def derive_tier(company: Company, settings: GlobalSettings) -> str:
    """Pure function -- takes the settings row explicitly rather than a
    db session, so callers (backfill CLI, scoring_service) control
    exactly which settings snapshot is used and this stays trivially
    testable without a database."""
    total_filings = _total_filings(company)
    wage_rank = WAGE_LEVEL_RANK.get(company.max_wage_level_15xx, 0)

    if total_filings >= settings.tier_a_min_filings and wage_rank >= settings.tier_a_min_wage_level:
        return "A"
    if total_filings >= settings.tier_b_min_filings:
        return "B"
    if total_filings >= 1:
        return "C"
    if company.is_cap_exempt:
        return "C"
    return "X"
