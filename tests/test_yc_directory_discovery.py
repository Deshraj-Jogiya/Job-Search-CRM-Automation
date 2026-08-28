"""Parsing logic for YC's public company-directory API pages -- pure,
no network call. The real page shape (captured directly from
api.ycombinator.com/v0.1/companies?isHiring=true during research) nests
companies under a "companies" list, each with a "name" field among many
others this app doesn't need.
"""

from app.services.yc_directory_discovery import parse_company_names

_SAMPLE_PAGE = {
    "companies": [
        {"id": 30090, "name": "Origami", "slug": "origami-2", "status": "Active"},
        {"id": 531, "name": "DoorDash", "slug": "doordash", "status": "Public"},
        {"id": 1, "name": "", "slug": "blank-name", "status": "Active"},
        {"id": 2, "name": "  Padded Co  ", "slug": "padded-co", "status": "Active"},
    ],
    "nextPage": "https://api.ycombinator.com/v0.1/companies?isHiring=true&page=2",
    "page": 1,
    "totalPages": 50,
}


def test_extracts_real_company_names():
    names = parse_company_names(_SAMPLE_PAGE)
    assert "Origami" in names
    assert "DoorDash" in names


def test_skips_a_blank_name():
    names = parse_company_names(_SAMPLE_PAGE)
    assert "" not in names
    assert len(names) == 3


def test_strips_whitespace_from_a_name():
    names = parse_company_names(_SAMPLE_PAGE)
    assert "Padded Co" in names
    assert "  Padded Co  " not in names


def test_missing_companies_key_returns_empty():
    assert parse_company_names({}) == []


def test_non_dict_entries_in_companies_list_are_skipped():
    page = {"companies": [{"name": "Real Co"}, "not-a-dict", None, 42]}
    assert parse_company_names(page) == ["Real Co"]
