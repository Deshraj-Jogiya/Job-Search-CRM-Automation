"""b1.1: the one-page resume-fit auto-reduction loop. Real content is
rendered and measured via reportlab (no mocking of the measurement
itself) -- these tests exercise the real loop against deliberately
small/oversized resumes, not a fake "page count" signal."""

from datetime import date

import pytest

from app.models import AdaptationLog, AdaptiveParameterValue
from app.services import page_fit_service

_LONG_BULLET = (
    "Did a substantial thing involving real detail, context, outcomes, and metrics, "
    "written long enough to consume real vertical space when rendered."
)


def _content(num_roles=1, bullets_per_role=2, num_projects=1, summary_repeats=1):
    # Every role ends "Present" -- classify_role (wired into both the
    # docx and PDF renderers) always treats a currently-held role as
    # EXPERIENCE regardless of tenure, so this reliably produces
    # num_roles full entries no matter how many are asked for. A fixed
    # start year old enough to be irrelevant to classification (only
    # the "Present" end matters); overlapping start dates are fine here
    # since this fixture is exercising page-fit overflow, not the
    # concurrent-overlap detector.
    this_year = date.today().year
    experience = [
        {
            "role": f"Role {i}", "company": f"Company {i}", "location": "Remote",
            "date": f"{this_year - 1} - Present",
            "bullets": [_LONG_BULLET] * bullets_per_role,
        }
        for i in range(num_roles)
    ]
    return {
        "name": "Test Candidate",
        "title": "Data Engineer",
        "contact": {"email": "t@example.com", "phone": "555-1234", "location": "Austin, TX"},
        "summary": "A concise summary. " * summary_repeats,
        "skills": {"Languages": ["Python", "SQL"]},
        "experience": experience,
        "projects": [{"name": f"Project {i}", "bullets": [_LONG_BULLET]} for i in range(num_projects)],
        "education": [{"degree": "B.S. Computer Science", "school": "State University", "date": "2015"}],
        "certifications": ["Cert A"],
    }


class TestFitsAlready:
    def test_small_content_needs_no_reductions(self, db):
        result = page_fit_service.render_resume_pdf_with_fit(db, _content(num_roles=1, bullets_per_role=1))
        assert result["page_fit_ok"] is True
        assert result["reductions_applied"] == []
        assert result["pdf_bytes"]


class TestOverflowingContentGetsReduced:
    def test_large_content_applies_reductions_and_still_produces_a_pdf(self, db):
        large = _content(num_roles=6, bullets_per_role=4, num_projects=3, summary_repeats=8)
        result = page_fit_service.render_resume_pdf_with_fit(db, large)
        assert result["page_fit_ok"] is True
        assert len(result["reductions_applied"]) > 0
        assert result["pdf_bytes"]

    def test_cold_start_tries_catalog_order_first(self, db):
        large = _content(num_roles=6, bullets_per_role=4, num_projects=3, summary_repeats=8)
        result = page_fit_service.render_resume_pdf_with_fit(db, large)
        # config's catalog lists tighten_line_spacing first -- with zero
        # learned history anywhere, that's what the very first pick must be.
        assert result["reductions_applied"][0] == "tighten_line_spacing"

    def test_each_real_reduction_is_logged(self, db):
        large = _content(num_roles=6, bullets_per_role=4, num_projects=3, summary_repeats=8)
        page_fit_service.render_resume_pdf_with_fit(db, large)
        entries = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "page_fit").all()
        assert len(entries) > 0
        assert all(e.tier == "tier1" and e.status == "applied" for e in entries)

    def test_learned_average_persists_across_calls(self, db):
        large = _content(num_roles=6, bullets_per_role=4, num_projects=3, summary_repeats=8)
        page_fit_service.render_resume_pdf_with_fit(db, large)
        row = (
            db.query(AdaptiveParameterValue)
            .filter(AdaptiveParameterValue.parameter == "page_fit_sample_count:tighten_line_spacing")
            .first()
        )
        assert row is not None
        assert row.value >= 1


class TestNeverTouchesProtectedContent:
    def test_education_is_never_modified(self, db):
        large = _content(num_roles=6, bullets_per_role=4, num_projects=3, summary_repeats=8)
        original_education = large["education"][0]["school"]
        page_fit_service.render_resume_pdf_with_fit(db, large)
        assert large["education"][0]["school"] == original_education

    def test_drop_weakest_bullet_never_removes_the_first_bullet(self, db):
        content = _content(num_roles=1, bullets_per_role=5, num_projects=1, summary_repeats=1)
        first_bullet_text = content["experience"][0]["bullets"][0]
        reduced = page_fit_service._apply_content_reduction(content, "drop_weakest_bullet_per_role", page_fit_service.resume_rules.get_config())
        assert reduced["experience"][0]["bullets"][0] == first_bullet_text
        assert len(reduced["experience"][0]["bullets"]) == 4

    def test_drop_third_project_keeps_only_first_two(self, db):
        content = _content(num_projects=3)
        config = page_fit_service.resume_rules.get_config()
        reduced = page_fit_service._apply_content_reduction(content, "drop_third_project", config)
        assert len(reduced["projects"]) == 2
        assert reduced["projects"][0]["name"] == "Project 0"
        assert reduced["projects"][1]["name"] == "Project 1"


class TestFailsLoudlyWhenExhausted:
    def test_impossibly_large_content_raises_with_diagnostics(self, db):
        huge = _content(num_roles=20, bullets_per_role=6, num_projects=5, summary_repeats=10)
        with pytest.raises(page_fit_service.PageFitExhaustedError) as exc:
            page_fit_service.render_resume_pdf_with_fit(db, huge)
        err = exc.value
        assert err.lines_over > 0
        assert err.target_lines > 0
        assert len(err.reductions_applied) > 0
        assert len(err.longest_bullets) > 0
        assert "overflows" in str(err)

    def test_never_shrinks_font_below_the_config_floor(self, db):
        huge = _content(num_roles=20, bullets_per_role=6, num_projects=5, summary_repeats=10)
        with pytest.raises(page_fit_service.PageFitExhaustedError):
            page_fit_service.render_resume_pdf_with_fit(db, huge)
        config = page_fit_service.resume_rules.get_config()
        floor = config["page_fit"]["min_body_font_pt"]
        row = db.query(AdaptiveParameterValue).filter(AdaptiveParameterValue.parameter.like("%shrink_body_font%")).first()
        # the loop itself never persists font size directly (only
        # lines-saved averages) -- this test instead confirms the
        # exhaustion check's own floor logic via a direct unit call.
        params = page_fit_service._RenderParams(
            body_font_pt=floor, line_leading_pt=floor + 1, margin_top_in=0.4, margin_bottom_in=0.35, margin_side_in=0.55,
        )
        assert page_fit_service._is_reduction_exhausted("shrink_body_font_one_step", huge, params, config) is True


class TestVisualReductionFloors:
    def test_tighten_line_spacing_stops_at_font_plus_point_three(self, db):
        config = page_fit_service.resume_rules.get_config()
        params = page_fit_service._RenderParams(
            body_font_pt=8.7, line_leading_pt=9.0, margin_top_in=0.4, margin_bottom_in=0.35, margin_side_in=0.55,
        )
        new_params = page_fit_service._apply_visual_reduction(params, "tighten_line_spacing", config)
        assert new_params.line_leading_pt >= params.body_font_pt + 0.3

    def test_shrink_margins_never_crosses_the_configured_floor(self, db):
        config = page_fit_service.resume_rules.get_config()
        floor = config["page_fit"]["min_margin_in"]
        params = page_fit_service._RenderParams(
            body_font_pt=8.7, line_leading_pt=10.3, margin_top_in=floor + 0.01,
            margin_bottom_in=floor, margin_side_in=floor,
        )
        new_params = page_fit_service._apply_visual_reduction(params, "shrink_margins_one_step", config)
        assert new_params.margin_top_in >= floor
        assert new_params.margin_bottom_in >= floor
        assert new_params.margin_side_in >= floor

    def test_shrink_body_font_never_crosses_the_configured_floor(self, db):
        config = page_fit_service.resume_rules.get_config()
        floor = config["page_fit"]["min_body_font_pt"]
        params = page_fit_service._RenderParams(
            body_font_pt=floor + 0.1, line_leading_pt=floor + 1.5, margin_top_in=0.4, margin_bottom_in=0.35, margin_side_in=0.55,
        )
        new_params = page_fit_service._apply_visual_reduction(params, "shrink_body_font_one_step", config)
        assert new_params.body_font_pt >= floor
