"""Location exclusion matching used by every direct-board source
(Greenhouse/Lever/Ashby have no location search param at all, so this is
the only thing standing between a US-based candidate and a country-only
posting -- see keyword_matching.location_allowed). Fails open by design:
absence of location data should never silently drop a posting, only a
positive match should.
"""

from app.services.sources.keyword_matching import location_allowed, location_eligible_for_scoring


def test_excludes_a_country_only_posting():
    assert location_allowed("Remote - Poland", ["Poland", "India"]) is False


def test_allows_a_us_posting_with_no_excluded_term_present():
    assert location_allowed("Tempe, AZ", ["Poland", "India"]) is True


def test_allows_when_location_is_missing_entirely():
    assert location_allowed(None, ["Poland", "India"]) is True


def test_allows_when_no_exclusions_are_configured():
    assert location_allowed("Remote - Poland", []) is True


def test_word_boundary_prevents_a_short_term_matching_inside_another_word():
    # "Chad" (a country) must not match "Chadwick" or similar --
    # substring matching would produce exactly this false positive.
    assert location_allowed("Chadwick, VA", ["Chad"]) is True


def test_match_is_case_insensitive():
    assert location_allowed("remote - poland", ["Poland"]) is False


# location_eligible_for_scoring: the deny-list-first gate used right
# before a real paid/subscription LLM call (scoring/tailoring), added
# 2026-09-24. A first, allow-list-based version of this function was run
# once against the live backlog and incorrectly excluded 458 real US
# applications whose location strings simply didn't contain a recognized
# US/Canada token (a bare "US", a bare city name like "San Francisco", a
# "City, County Name" shape like "Chantilly, Fairfax County") -- an
# allow-list can never fully enumerate every real location string. This
# version only ever excludes on a positive match against a real deny-list
# term (same shape as location_allowed), and defaults to None ("can't
# tell, let it through") for everything else, including the exact real
# strings that caused the original incident.
def test_scoring_gate_returns_none_for_a_city_only_posting_with_no_country_token():
    # "Coimbatore, Tamil Nadu" (India) contains no deny-list term at all --
    # ambiguous, must not be excluded.
    assert location_eligible_for_scoring("Coimbatore, Tamil Nadu", deny_terms=["India"]) is None


def test_scoring_gate_excludes_real_non_us_examples_found_live():
    deny = ["India", "Brazil", "South Korea", "United Kingdom"]
    assert location_eligible_for_scoring("India - Bengaluru", deny_terms=deny) is False
    assert location_eligible_for_scoring("Brazil - Sao Paulo", deny_terms=deny) is False
    assert location_eligible_for_scoring("Pangyo (Software Dream Center), South Korea", deny_terms=deny) is False
    assert location_eligible_for_scoring("London, England, United Kingdom", deny_terms=deny) is False


def test_scoring_gate_excludes_multi_location_string_with_a_deny_term_present():
    assert location_eligible_for_scoring("India - Bengaluru; India - Pune", deny_terms=["India"]) is False


def test_scoring_gate_allows_multi_location_string_with_at_least_one_us_segment():
    assert location_eligible_for_scoring("New York City, NY | Seattle, WA; San Francisco, CA", deny_terms=[]) is True


def test_scoring_gate_allows_a_real_us_city_state_posting():
    assert location_eligible_for_scoring("Seattle, WA", deny_terms=[]) is True
    assert location_eligible_for_scoring("Tempe, AZ", deny_terms=[]) is True


def test_scoring_gate_allows_a_real_canada_posting():
    assert location_eligible_for_scoring("Toronto", deny_terms=[]) is True
    assert location_eligible_for_scoring("Remote - Canada", deny_terms=[]) is True


def test_scoring_gate_returns_none_for_bare_remote_with_no_country_qualifier():
    # Many US companies post exactly "Remote" with US-only eligibility --
    # this must not auto-exclude, only flag as ambiguous (callers let it
    # through, matching JobPosting.worksite_ambiguous).
    assert location_eligible_for_scoring("Remote", deny_terms=[]) is None
    assert location_eligible_for_scoring("Remote ", deny_terms=[]) is None


def test_scoring_gate_returns_none_for_missing_location():
    assert location_eligible_for_scoring(None) is None
    assert location_eligible_for_scoring("") is None


def test_scoring_gate_excludes_state_abbreviation_country_mismatch_via_deny_term():
    # "Remote - India" is caught by the deny-list term itself, not by
    # state-abbreviation logic.
    assert location_eligible_for_scoring("Remote - India", deny_terms=["India"]) is False


def test_scoring_gate_never_excludes_real_us_strings_that_caused_the_2026_09_24_incident():
    # Exact real JobPosting.location strings that the first, allow-list
    # version of this function incorrectly excluded. None of these should
    # ever return False -- they contain no real deny-list term.
    real_us_strings = [
        "Chantilly, Fairfax County",
        "Raleigh, Smith County",
        "US",
        "Northlake, DeKalb County",
        "Woodlawn, Baltimore County",
        "Capitol, Richmond",
        "San Francisco",
        "Plano, Collin County",
        "Bee Cave, Travis County",
        "Fort Meade, Meade County",
    ]
    for location in real_us_strings:
        assert location_eligible_for_scoring(location, deny_terms=[]) is not False
