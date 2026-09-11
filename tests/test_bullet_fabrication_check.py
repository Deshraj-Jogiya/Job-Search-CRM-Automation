"""Unit coverage for tailoring_service.check_bullet_fabrication -- the
general bullet-prose fabrication check FUTURE.md documented as a real,
previously-open gap (an invented metric/scope/outcome written directly
into a bullet's prose, not just an over-claimed years figure or a bare
unverified percentage -- see resume_rules.py's D1/D2 for those
narrower, non-LLM checks). Mocks the LLM call directly; wiring into
tailor_application() is covered separately in
test_tailor_application_prose_checks.py."""

from unittest.mock import Mock, patch

from app.services.tailoring_service import check_bullet_fabrication

_ORIGINAL_EXPERIENCE = [
    {"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["Built ingestion pipelines."]},
]
_ORIGINAL_PROJECTS = [{"name": "Real Project", "bullets": ["Wrote ETL scripts."]}]


def _mock_llm(response_json: str):
    provider = Mock()
    provider.complete_json.return_value = response_json
    return patch("app.services.tailoring_service.get_llm_provider", return_value=provider)


class TestCheckBulletFabrication:
    def test_returns_empty_list_when_llm_finds_nothing(self):
        with _mock_llm('{"fabrications": []}'):
            result = check_bullet_fabrication(
                _ORIGINAL_EXPERIENCE, _ORIGINAL_EXPERIENCE, _ORIGINAL_PROJECTS, _ORIGINAL_PROJECTS, "JD text",
            )
        assert result == []

    def test_returns_the_llms_flagged_fabrications(self):
        with _mock_llm('{"fabrications": ["Entry 0 bullet 0: claims leading a team of 12, not in the original"]}'):
            tailored = [
                {"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["Led a team of 12 engineers."]},
            ]
            result = check_bullet_fabrication(_ORIGINAL_EXPERIENCE, tailored, _ORIGINAL_PROJECTS, _ORIGINAL_PROJECTS, "JD text")
        assert result == ["Entry 0 bullet 0: claims leading a team of 12, not in the original"]

    def test_missing_fabrications_key_defaults_to_empty(self):
        with _mock_llm('{}'):
            result = check_bullet_fabrication(
                _ORIGINAL_EXPERIENCE, _ORIGINAL_EXPERIENCE, _ORIGINAL_PROJECTS, _ORIGINAL_PROJECTS, "JD text",
            )
        assert result == []

    def test_prompt_includes_both_original_and_tailored_content(self):
        provider = Mock()
        provider.complete_json.return_value = '{"fabrications": []}'
        tailored = [
            {"role": "Data Engineer", "company": "Real Co", "date": "Jan 2024 - Jun 2024", "bullets": ["A distinctive tailored bullet marker."]},
        ]
        with patch("app.services.tailoring_service.get_llm_provider", return_value=provider):
            check_bullet_fabrication(_ORIGINAL_EXPERIENCE, tailored, _ORIGINAL_PROJECTS, _ORIGINAL_PROJECTS, "JD text")
        prompt = provider.complete_json.call_args.kwargs["prompt"]
        assert "Built ingestion pipelines." in prompt  # original
        assert "A distinctive tailored bullet marker." in prompt  # tailored
