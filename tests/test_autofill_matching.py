"""Pure logic from the autofill modules -- profile-to-field-value
mapping and drafted-answer-to-real-option matching. None of this needs
a live browser or a live LLM call, so it's covered here instead of only
by manual QA against a real posting.

The option matchers are the safety-critical piece: every ATS module
uses the same match-or-leave-for-human policy rather than ever guessing
a close-but-wrong option, and that policy is exactly what's under test.
"""

from unittest.mock import MagicMock, patch

from app.services import autofill_service
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


class TestDraftCustomAnswersDegradesGracefully:
    """A malformed LLM response here previously raised past this
    function, propagated through the whole autofill_*_application call,
    and killed the entire real, in-progress, already-partially-filled
    browser session -- caught for real via a live Ashby autofill run
    whose custom-question drafting call returned valid JSON followed by
    trailing content. Every ATS module shares this exact call shape, so
    all three get the same coverage."""

    _questions = [{"field_path": "q1", "name": "q1", "id": "q1", "field_type": "text", "label": "Why us?"}]

    def _fake_llm(self, raw_response):
        provider = MagicMock()
        provider.complete_json.return_value = raw_response
        return provider

    def test_ashby_returns_empty_dict_instead_of_raising(self):
        with patch("app.services.autofill.ashby_autofill.get_llm_provider", return_value=self._fake_llm("not json")):
            assert ashby_autofill._draft_custom_answers(self._questions, {}, "jd text", "Acme") == {}

    def test_lever_returns_empty_dict_instead_of_raising(self):
        with patch("app.services.autofill.lever_autofill.get_llm_provider", return_value=self._fake_llm("not json")):
            assert lever_autofill._draft_custom_answers(self._questions, {}, "jd text", "Acme") == {}

    def test_greenhouse_returns_empty_dict_instead_of_raising(self):
        with patch(
            "app.services.autofill.greenhouse_autofill.get_llm_provider", return_value=self._fake_llm("not json")
        ):
            assert greenhouse_autofill._draft_custom_answers(self._questions, {}, "jd text", "Acme") == {}

    def test_ashby_still_returns_real_answers_on_a_clean_response(self):
        clean = '{"answers": ["Because I love the mission."]}'
        with patch("app.services.autofill.ashby_autofill.get_llm_provider", return_value=self._fake_llm(clean)):
            result = ashby_autofill._draft_custom_answers(self._questions, {}, "jd text", "Acme")
        assert result == {"q1": "Because I love the mission."}


class FakeFrame:
    """Stands in for a Playwright Frame -- just enough surface
    (.url, .page) to test _resolve_fill_scope/_owning_page without a
    real browser."""

    def __init__(self, url, owning_page=None):
        self.url = url
        if owning_page is not None:
            self.page = owning_page


class FakeTopPage:
    """Stands in for a Playwright Page -- .frames includes a synthetic
    main_frame entry (matching real Playwright, where page.frames[0] is
    always the main frame) plus whatever nested frames the test needs."""

    def __init__(self, url, nested_frames=()):
        self.url = url
        self.main_frame = FakeFrame(url)
        self.frames = [self.main_frame, *nested_frames]


class TestResolveFillScope:
    """Real, live gap: an employer embedding an ATS's form via iframe
    on their own branded careers page (confirmed live -- Samsara embeds
    a Greenhouse form at samsara.com/company/careers/..., not
    job-boards.greenhouse.io) left every .locator() call in the
    autofill modules searching the top-level page for fields that only
    existed inside the iframe -- a silent zero-fields-filled result,
    no exception. Matched by the ATS's own real hostname so this
    generalizes to Lever/Ashby too, not just the one embed pattern
    already confirmed live."""

    def test_already_on_the_ats_own_hosted_board_returns_the_page_unchanged(self):
        page = FakeTopPage("https://job-boards.greenhouse.io/acme/jobs/123")
        assert autofill_service._resolve_fill_scope(page, "greenhouse") is page

    def test_subdomain_of_the_ats_own_host_also_counts_as_the_page(self):
        page = FakeTopPage("https://boards.greenhouse.io/acme/jobs/123")
        assert autofill_service._resolve_fill_scope(page, "greenhouse") is page

    def test_embedded_iframe_on_the_employers_own_domain_is_detected(self):
        embed_frame = FakeFrame("https://job-boards.greenhouse.io/embed/job_app?for=acme&token=xyz")
        page = FakeTopPage("https://www.acme.com/careers/123?gh_jid=123", nested_frames=[embed_frame])
        assert autofill_service._resolve_fill_scope(page, "greenhouse") is embed_frame

    def test_lever_embed_is_also_detected_not_just_greenhouse(self):
        # The mechanism is generic (hostname-matched), not hardcoded to
        # the one embed pattern already confirmed live for Greenhouse.
        embed_frame = FakeFrame("https://jobs.lever.co/acme/embed/abc123")
        page = FakeTopPage("https://www.acme.com/careers/123", nested_frames=[embed_frame])
        assert autofill_service._resolve_fill_scope(page, "lever") is embed_frame

    def test_no_matching_frame_falls_back_to_the_page(self):
        unrelated_frame = FakeFrame("https://analytics.example.com/tracker.html")
        page = FakeTopPage("https://www.acme.com/careers/123", nested_frames=[unrelated_frame])
        assert autofill_service._resolve_fill_scope(page, "greenhouse") is page

    def test_unknown_source_falls_back_to_the_page(self):
        page = FakeTopPage("https://www.acme.com/careers/123")
        assert autofill_service._resolve_fill_scope(page, "some_future_source") is page


class TestOwningPageHelper:
    """Each autofill module's own _owning_page: a Frame's real file-
    chooser/keyboard calls have no per-frame equivalent in Playwright,
    so these must resolve back to the frame's real owning Page."""

    def test_greenhouse_owning_page_passes_through_a_real_page(self):
        page = FakeTopPage("https://job-boards.greenhouse.io/acme/jobs/1")
        assert greenhouse_autofill._owning_page(page) is page

    def test_greenhouse_owning_page_resolves_a_frame_to_its_page(self):
        page = FakeTopPage("https://www.acme.com/careers")
        frame = FakeFrame("https://job-boards.greenhouse.io/embed/job_app", owning_page=page)
        assert greenhouse_autofill._owning_page(frame) is page

    def test_lever_owning_page_resolves_a_frame_to_its_page(self):
        page = FakeTopPage("https://www.acme.com/careers")
        frame = FakeFrame("https://jobs.lever.co/acme/embed/1", owning_page=page)
        assert lever_autofill._owning_page(frame) is page

    def test_ashby_owning_page_resolves_a_frame_to_its_page(self):
        page = FakeTopPage("https://www.acme.com/careers")
        frame = FakeFrame("https://jobs.ashbyhq.com/acme/embed/1", owning_page=page)
        assert ashby_autofill._owning_page(frame) is page
