"""Location exclusion matching used by every direct-board source
(Greenhouse/Lever/Ashby have no location search param at all, so this is
the only thing standing between a US-based candidate and a country-only
posting -- see keyword_matching.location_allowed). Fails open by design:
absence of location data should never silently drop a posting, only a
positive match should.
"""

from app.services.sources.keyword_matching import location_allowed


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
