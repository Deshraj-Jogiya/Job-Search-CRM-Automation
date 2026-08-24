"""Pure logic from the autofill modules -- profile-to-field-value
mapping and drafted-answer-to-real-option matching. None of this needs
a live browser or a live LLM call, so it's covered here instead of only
by manual QA against a real posting.

The option matchers are the safety-critical piece: every ATS module
uses the same match-or-leave-for-human policy rather than ever guessing
a close-but-wrong option, and that policy is exactly what's under test.
"""

from app.services.autofill import ashby_autofill, greenhouse_autofill, lever_autofill


class FakePage:
    """Stands in for a Playwright Page in the one Lever function that
    needs to read the DOM for posting-specific field names -- lets
    _standard_field_values be tested without a real browser."""

    def __init__(self, url_field_names):
        self._url_field_names = url_field_names

    def evaluate(self, _script):
        return self._url_field_names


def test_greenhouse_standard_fields_split_full_name_and_omit_blanks():
    profile = {"name": "Jordan Ellis", "contact": {"email": "jordan@example.com"}}

    values = greenhouse_autofill._standard_field_values(profile)

    assert values == {"first_name": "Jordan", "last_name": "Ellis", "email": "jordan@example.com"}


def test_greenhouse_education_only_maps_school_and_degree():
    profile = {"education": [{"school": "State University", "degree": "B.S. Computer Science", "date": "2020"}]}

    values = greenhouse_autofill._education_field_values(profile)

    assert values == {"school--0": "State University", "degree--0": "B.S. Computer Science"}


def test_greenhouse_education_empty_when_profile_has_none():
    assert greenhouse_autofill._education_field_values({}) == {}


def test_lever_standard_fields_never_include_location():
    profile = {"name": "Jordan Ellis", "contact": {"email": "jordan@example.com", "location": "Austin, TX"}}
    page = FakePage(url_field_names=[])

    values = lever_autofill._standard_field_values(page, profile)

    assert "location" not in values
    assert values["name"] == "Jordan Ellis"


def test_lever_url_fields_matched_by_keyword_in_the_posting_specific_name():
    profile = {
        "name": "Jordan Ellis",
        "contact": {"linkedin": "https://linkedin.com/in/jordan", "github": "https://github.com/jordan"},
    }
    page = FakePage(url_field_names=["urls[LinkedIn (optional)]", "urls[GitHub (optional)]", "urls[Other]"])

    values = lever_autofill._standard_field_values(page, profile)

    assert values["urls[LinkedIn (optional)]"] == "https://linkedin.com/in/jordan"
    assert values["urls[GitHub (optional)]"] == "https://github.com/jordan"
    assert "urls[Other]" not in values  # nothing in the profile to match it against


def test_ashby_standard_fields_use_system_field_ids():
    profile = {"name": "Jordan Ellis", "contact": {"email": "jordan@example.com"}}

    values = ashby_autofill._standard_field_values(profile)

    assert values == {"_systemfield_name": "Jordan Ellis", "_systemfield_email": "jordan@example.com"}


def test_option_matcher_prefers_exact_case_insensitive_match():
    idx = lever_autofill._match_option("yes", ["No", "Yes", "Maybe"])
    assert idx == 1


def test_option_matcher_falls_back_to_substring_match():
    idx = lever_autofill._match_option("Arizona State University", ["Arizona State University - West", "Other"])
    assert idx == 0


def test_option_matcher_returns_none_rather_than_guess_when_nothing_matches():
    assert lever_autofill._match_option("Purple Elephant University", ["MIT", "Stanford"]) is None


def test_option_matcher_never_selects_review_needed_placeholder():
    assert lever_autofill._match_option("[REVIEW NEEDED]", ["Yes", "No"]) is None


def test_option_matcher_skips_the_select_placeholder_option():
    # "Select..." is the literal placeholder Lever renders as option 0;
    # an LLM answer should never resolve to it even on a loose match.
    idx = lever_autofill._match_option("select", ["Select...", "Yes", "No"])
    assert idx is None


def test_ashby_option_matcher_shares_the_same_conservative_policy():
    assert ashby_autofill._match_option("Yes", ["Yes", "No"]) == 0
    assert ashby_autofill._match_option("[REVIEW NEEDED]", ["Yes", "No"]) is None
    assert ashby_autofill._match_option("", ["Yes", "No"]) is None
