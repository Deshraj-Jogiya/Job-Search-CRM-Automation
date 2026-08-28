"""Parsing logic for the job-board-aggregator open dataset's bare-slug
JSON lists -- pure, no network call. Real captured samples (see
job_board_aggregator_discovery.py's docstring) include non-name
artifacts (bare numeric IDs, garbled strings) mixed into otherwise
real slugs -- those must be filtered, not just passed through.
"""

from app.services.job_board_aggregator_discovery import (
    parse_slug_list,
    slug_to_provisional_name,
)

_SAMPLE_SLUGS = ["0x", "100x", "103644278", "10alabs", "1456754456yhgbhfg", "10up"]


def test_keeps_plausible_company_slugs():
    result = parse_slug_list(_SAMPLE_SLUGS)
    assert "0x" in result
    assert "100x" in result
    assert "10alabs" in result
    assert "10up" in result


def test_drops_bare_numeric_artifacts():
    result = parse_slug_list(_SAMPLE_SLUGS)
    assert "103644278" not in result


def test_non_list_input_returns_empty():
    assert parse_slug_list({"not": "a list"}) == []
    assert parse_slug_list(None) == []


def test_non_string_entries_in_the_list_are_skipped():
    result = parse_slug_list(["acme", 12345, None, "beta-labs"])
    assert result == ["acme", "beta-labs"]


def test_slug_to_provisional_name_hyphens_and_underscores():
    assert slug_to_provisional_name("acme-labs") == "Acme Labs"
    assert slug_to_provisional_name("beta_corp") == "Beta Corp"


def test_slug_to_provisional_name_single_word():
    assert slug_to_provisional_name("stripe") == "Stripe"
