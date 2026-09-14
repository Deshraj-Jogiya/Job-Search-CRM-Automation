"""Trend research: bounded, real-search-backed re-checks of specific
resume-building config values -- and the hard rule that a finding is
ALWAYS a proposed AdaptationLog row a human must approve, never a
silent config write. Mocks the network (Tavily) and the LLM the same
way every other external-API integration in this suite does; the write
tests use a real temp YAML file, never the project's real
config/resume_rules.yaml."""

import json
from unittest.mock import patch

import pytest

from app.models import AdaptationLog
from app.services import trend_research_service as trs


_SAMPLE_RESULTS = [
    {"title": "2026 Resume Length Guide", "url": "https://example.com/a", "content": "Two pages are now preferred for substantial experience."},
    {"title": "Recruiter Survey", "url": "https://example.com/b", "content": "Recruiters favor two-page resumes 2.3x more often."},
]


def _llm_json(payload: dict) -> str:
    return json.dumps(payload)


class TestRunTrendCheck:
    def test_skips_when_tavily_not_configured(self, db):
        with patch.object(trs, "is_tavily_configured", return_value=False):
            result = trs.run_trend_check(db, "resume_max_pages", 2026)
        assert result is None
        assert db.query(AdaptationLog).count() == 0

    def test_skips_when_a_proposal_is_already_pending(self, db):
        from app.services.adaptation_service import log_adaptation
        log_adaptation(db, "tier2", trs._SUBSYSTEM, "resume_max_pages", 1, 2, status="proposed")
        with patch.object(trs, "is_tavily_configured", return_value=True):
            result = trs.run_trend_check(db, "resume_max_pages", 2026)
        assert result is None
        assert db.query(AdaptationLog).filter(AdaptationLog.subsystem == trs._SUBSYSTEM).count() == 1

    def test_creates_a_proposal_when_recommendation_meaningfully_differs(self, db):
        with patch.object(trs, "is_tavily_configured", return_value=True), \
             patch.object(trs, "tavily_search", return_value=_SAMPLE_RESULTS), \
             patch.object(trs, "_extract_recommendation") as mock_extract:
            mock_extract.return_value = {
                "has_clear_recommendation": True,
                "recommended_value": 2,
                "confidence": "high",
                "summary": "Sources favor two-page resumes for substantial experience.",
                "source_indices_used": [1, 2],
            }
            with patch.object(trs.TREND_CATEGORIES["resume_max_pages"], "current_value_fn", return_value=1):
                result = trs.run_trend_check(db, "resume_max_pages", 2026)
        assert result is not None
        assert result.status == "proposed"
        assert result.tier == "tier2"
        assert result.subsystem == trs._SUBSYSTEM
        assert result.old_value == 1
        assert result.new_value == 2
        assert result.triggering_evidence["summary"]
        assert len(result.triggering_evidence["sources"]) == 2

    def test_no_proposal_when_recommendation_matches_current_value(self, db):
        with patch.object(trs, "is_tavily_configured", return_value=True), \
             patch.object(trs, "tavily_search", return_value=_SAMPLE_RESULTS), \
             patch.object(trs, "_extract_recommendation") as mock_extract:
            mock_extract.return_value = {
                "has_clear_recommendation": True,
                "recommended_value": 2,
                "confidence": "high",
                "summary": "Two pages still checks out.",
                "source_indices_used": [1],
            }
            with patch.object(trs.TREND_CATEGORIES["resume_max_pages"], "current_value_fn", return_value=2):
                result = trs.run_trend_check(db, "resume_max_pages", 2026)
        assert result is None
        assert db.query(AdaptationLog).count() == 0

    def test_no_proposal_when_llm_finds_no_clear_recommendation(self, db):
        with patch.object(trs, "is_tavily_configured", return_value=True), \
             patch.object(trs, "tavily_search", return_value=_SAMPLE_RESULTS), \
             patch.object(trs, "_extract_recommendation", return_value=None):
            result = trs.run_trend_check(db, "resume_max_pages", 2026)
        assert result is None
        assert db.query(AdaptationLog).count() == 0

    def test_search_failure_is_handled_gracefully_not_raised(self, db):
        with patch.object(trs, "is_tavily_configured", return_value=True), \
             patch.object(trs, "tavily_search", side_effect=Exception("network error")):
            result = trs.run_trend_check(db, "resume_max_pages", 2026)
        assert result is None
        assert db.query(AdaptationLog).count() == 0


class TestExtractRecommendation:
    def test_returns_none_for_empty_results(self):
        assert trs._extract_recommendation("q", [], 1) is None

    def test_never_invents_a_number_when_llm_says_no_clear_recommendation(self):
        with patch.object(trs, "get_llm_provider") as mock_provider:
            mock_provider.return_value.complete_json.return_value = _llm_json({
                "has_clear_recommendation": False, "recommended_value": None,
                "summary": "Sources are conflicting.", "source_indices_used": [],
            })
            result = trs._extract_recommendation("q", _SAMPLE_RESULTS, 1)
        assert result is None

    def test_rejects_a_non_integer_recommended_value(self):
        with patch.object(trs, "get_llm_provider") as mock_provider:
            mock_provider.return_value.complete_json.return_value = _llm_json({
                "has_clear_recommendation": True, "recommended_value": "two",
                "summary": "x", "source_indices_used": [1],
            })
            result = trs._extract_recommendation("q", _SAMPLE_RESULTS, 1)
        assert result is None

    def test_malformed_llm_output_is_handled_not_raised(self):
        with patch.object(trs, "get_llm_provider") as mock_provider:
            mock_provider.return_value.complete_json.return_value = "not json at all"
            result = trs._extract_recommendation("q", _SAMPLE_RESULTS, 1)
        assert result is None


class TestApplyYamlChange:
    def _write_fixture_yaml(self, tmp_path, contents: str):
        path = tmp_path / "resume_rules.yaml"
        path.write_text(contents, encoding="utf-8")
        return path

    def test_replaces_the_target_line_preserving_everything_else(self, tmp_path):
        original = (
            "work_authorization:\n"
            "  include_work_auth_line: true\n"
            "\n"
            "page_fit:\n"
            "  # a real comment explaining the old value, must survive\n"
            "  max_pages: 1\n"
            "  min_body_font_pt: 8.0\n"
        )
        path = self._write_fixture_yaml(tmp_path, original)
        with patch.object(trs, "_RESUME_RULES_PATH", path):
            trs._apply_yaml_change(trs.TREND_CATEGORIES["resume_max_pages"], 2)
        result = path.read_text(encoding="utf-8")
        assert "max_pages: 2" in result
        assert "max_pages: 1" not in result
        assert "a real comment explaining the old value, must survive" in result
        assert "include_work_auth_line: true" in result
        assert "min_body_font_pt: 8.0" in result

    def test_raises_clearly_when_pattern_not_found(self, tmp_path):
        path = self._write_fixture_yaml(tmp_path, "some_other_key: 5\n")
        with patch.object(trs, "_RESUME_RULES_PATH", path):
            with pytest.raises(trs.TrendProposalError, match="Could not find"):
                trs._apply_yaml_change(trs.TREND_CATEGORIES["resume_max_pages"], 2)
        # File must be untouched on failure.
        assert path.read_text(encoding="utf-8") == "some_other_key: 5\n"

    def test_only_touches_the_real_config_line_not_a_comment_mentioning_it(self, tmp_path):
        original = (
            "page_fit:\n"
            "  # old max_pages: 1 was a stale unverified assumption\n"
            "  max_pages: 2\n"
        )
        path = self._write_fixture_yaml(tmp_path, original)
        with patch.object(trs, "_RESUME_RULES_PATH", path):
            trs._apply_yaml_change(trs.TREND_CATEGORIES["resume_max_pages"], 3)
        result = path.read_text(encoding="utf-8")
        assert "old max_pages: 1 was a stale unverified assumption" in result  # comment untouched
        assert "  max_pages: 3" in result


class TestApproveAndReject:
    def test_approve_applies_the_yaml_change_and_marks_approved(self, db, tmp_path):
        from app.services.adaptation_service import log_adaptation
        entry = log_adaptation(db, "tier2", trs._SUBSYSTEM, "resume_max_pages", 1, 2, status="proposed")

        path = tmp_path / "resume_rules.yaml"
        path.write_text("page_fit:\n  max_pages: 1\n", encoding="utf-8")

        with patch.object(trs, "_RESUME_RULES_PATH", path):
            approved = trs.approve_trend_proposal(db, entry.id)

        assert approved.status == "approved"
        assert "max_pages: 2" in path.read_text(encoding="utf-8")

    def test_approve_raises_for_unknown_log_id(self, db):
        with pytest.raises(trs.TrendProposalError):
            trs.approve_trend_proposal(db, 999999)

    def test_approve_raises_for_a_non_pending_proposal(self, db):
        from app.services.adaptation_service import log_adaptation
        entry = log_adaptation(db, "tier2", trs._SUBSYSTEM, "resume_max_pages", 1, 2, status="rejected")
        with pytest.raises(trs.TrendProposalError, match="not pending"):
            trs.approve_trend_proposal(db, entry.id)

    def test_approve_raises_for_a_proposal_from_a_different_subsystem(self, db):
        from app.services.adaptation_service import log_adaptation
        entry = log_adaptation(db, "tier2", "some_other_subsystem", "x", 1, 2, status="proposed")
        with pytest.raises(trs.TrendProposalError):
            trs.approve_trend_proposal(db, entry.id)

    def test_reject_marks_rejected_and_never_touches_the_yaml_file(self, db, tmp_path):
        from app.services.adaptation_service import log_adaptation
        entry = log_adaptation(db, "tier2", trs._SUBSYSTEM, "resume_max_pages", 1, 2, status="proposed")

        path = tmp_path / "resume_rules.yaml"
        path.write_text("page_fit:\n  max_pages: 1\n", encoding="utf-8")

        with patch.object(trs, "_RESUME_RULES_PATH", path):
            rejected = trs.reject_trend_proposal(db, entry.id)

        assert rejected.status == "rejected"
        assert "max_pages: 1" in path.read_text(encoding="utf-8")  # unchanged


class TestPendingTrendProposals:
    def test_returns_only_proposed_rows_for_this_subsystem(self, db):
        from app.services.adaptation_service import log_adaptation
        log_adaptation(db, "tier2", trs._SUBSYSTEM, "resume_max_pages", 1, 2, status="proposed")
        log_adaptation(db, "tier2", trs._SUBSYSTEM, "resume_project_count", 3, 4, status="approved")
        log_adaptation(db, "tier2", "comparison:resume_variant", "some_param", 1, 2, status="proposed")

        pending = trs.pending_trend_proposals(db)

        assert len(pending) == 1
        assert pending[0].parameter == "resume_max_pages"
