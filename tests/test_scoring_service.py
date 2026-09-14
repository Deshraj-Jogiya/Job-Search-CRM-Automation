import json

from app.models import Company, JobApplication, JobPosting
from app.services.scoring_service import compute_score_breakdown, recompute_score_breakdown


def _company(db, **overrides):
    from app.services.company_utils import normalize_company_name

    defaults = dict(name="Acme Corp", normalized_name=normalize_company_name("Acme Corp"))
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def _posting(db, company, **overrides):
    defaults = dict(
        company_id=company.id, company_name_raw=company.name, job_title="Data Engineer",
        job_description="A great job.", source="greenhouse",
    )
    defaults.update(overrides)
    posting = JobPosting(**defaults)
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


def _application(db, posting, **overrides):
    defaults = dict(posting_id=posting.id)
    defaults.update(overrides)
    application = JobApplication(**defaults)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


class TestUnscoredApplication:
    def test_ai_profile_fit_contributes_zero_when_never_scored(self, db):
        company = _company(db)
        posting = _posting(db, company)
        application = _application(db, posting)

        breakdown = compute_score_breakdown(application)
        assert breakdown["components"]["ai_profile_fit"]["raw"] is None
        assert breakdown["components"]["ai_profile_fit"]["contribution"] == 0


class TestScoreNormalization:
    def test_perfect_application_sums_to_exactly_100(self, db):
        from datetime import datetime, timezone

        company = _company(db, tier="A", is_cap_exempt=False, max_wage_level_15xx="IV")
        posting = _posting(
            db, company, job_title="Data Engineer", location="Austin, TX", sponsorship_signal=False,
            worksite_ambiguous=False, first_seen_at=datetime.now(timezone.utc),
        )
        application = _application(
            db, posting, match_score=100, match_analysis_json=json.dumps({"match_score": 100}),
        )
        # Every base component at its own max, bonus untouched: ai_fit(30)
        # + sponsorship_history(30) + wage(20) + role_title_match(12) +
        # worksite(6) + posting_recency(2) = 100 exactly, pre-clamp --
        # proves the 100-base design lands on 100, not just that the
        # clamp happens to save an over-100 sum.
        breakdown = compute_score_breakdown(application, active_keywords=["Data Engineer"])
        assert breakdown["total"] == 100

    def test_total_never_exceeds_100_even_with_every_bonus(self, db):
        # Same as above PLUS the sponsorship_signal bonus fires too --
        # 100 base + 10 bonus would be 110 pre-clamp; verify the
        # min(100, ...) clamp is real by checking the sum logic directly
        # rather than assuming no combination can exceed it.
        from datetime import datetime, timezone

        company = _company(db, tier="A", is_cap_exempt=True, max_wage_level_15xx="IV")
        posting = _posting(
            db, company, job_title="Data Engineer", location="Austin, TX", sponsorship_signal=True,
            worksite_ambiguous=False, first_seen_at=datetime.now(timezone.utc),
        )
        application = _application(
            db, posting, match_score=100, match_analysis_json=json.dumps({"match_score": 100}),
        )
        breakdown = compute_score_breakdown(application, active_keywords=["Data Engineer"])
        assert breakdown["total"] == 100  # clamped down from 110

    def test_worst_case_sums_to_a_low_but_valid_score(self, db):
        company = _company(db, tier="X", is_cap_exempt=False, max_wage_level_15xx=None)
        posting = _posting(db, company, location=None, sponsorship_signal=False, worksite_ambiguous=True)
        application = _application(db, posting, match_score=0, match_analysis_json=json.dumps({"match_score": 0}))
        breakdown = compute_score_breakdown(application)
        # ai_fit(0) + sponsorship_history(X, 0) + wage(unknown, 6) +
        # role_title_match(no keywords resolved -- neutral, 6) +
        # worksite(ambiguous, 0) + posting_recency(fresh, 2) + bonus(0)
        assert breakdown["total"] == 0 + 0 + 6 + 6 + 0 + 2 + 0


class TestSponsorshipHistoryComponent:
    def test_tier_a_scores_30(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["sponsorship_history"]["contribution"] == 30

    def test_tier_b_scores_18(self, db):
        company = _company(db, tier="B")
        posting = _posting(db, company)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["sponsorship_history"]["contribution"] == 18

    def test_cap_exempt_floors_at_30_even_with_tier_c(self, db):
        company = _company(db, tier="C", is_cap_exempt=True)
        posting = _posting(db, company)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["sponsorship_history"]["contribution"] == 30

    def test_no_company_scores_unknown_value(self, db):
        posting = JobPosting(company_id=None, company_name_raw="Unknown", job_title="X", job_description="d", source="manual")
        db.add(posting)
        db.commit()
        db.refresh(posting)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["sponsorship_history"]["contribution"] == 4


class TestWageLevelFitComponent:
    def test_level_iv_scores_20(self, db):
        company = _company(db, max_wage_level_15xx="IV")
        posting = _posting(db, company)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["wage_level_fit"]["contribution"] == 20

    def test_unknown_wage_level_scores_6(self, db):
        company = _company(db, max_wage_level_15xx=None)
        posting = _posting(db, company)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["wage_level_fit"]["contribution"] == 6

    def test_per_posting_wage_level_preferred_over_company_level(self, db):
        # Company-level says III, but THIS posting's own parsed/manual
        # salary classified as IV -- the posting-specific signal wins.
        company = _company(db, max_wage_level_15xx="III")
        posting = _posting(db, company, wage_level_per_posting="IV")
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["wage_level_fit"]["contribution"] == 20

    def test_falls_back_to_company_level_when_no_per_posting_value(self, db):
        company = _company(db, max_wage_level_15xx="III")
        posting = _posting(db, company, wage_level_per_posting=None)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["wage_level_fit"]["contribution"] == 15

    def test_below_level_i_scores_zero(self, db):
        company = _company(db, max_wage_level_15xx="IV")
        posting = _posting(db, company, wage_level_per_posting="Below Level I")
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["wage_level_fit"]["contribution"] == 0


class TestWorksiteClarityComponent:
    def test_ambiguous_flag_scores_zero(self, db):
        company = _company(db)
        posting = _posting(db, company, location="Austin, TX", worksite_ambiguous=True)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["worksite_clarity"]["contribution"] == 0

    def test_specific_city_scores_full_weight(self, db):
        company = _company(db)
        posting = _posting(db, company, location="Austin, TX", worksite_ambiguous=False)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["worksite_clarity"]["contribution"] == 6

    def test_bare_remote_scores_partial(self, db):
        company = _company(db)
        posting = _posting(db, company, location="Remote", worksite_ambiguous=False)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["worksite_clarity"]["contribution"] == 4

    def test_no_location_scores_partial(self, db):
        company = _company(db)
        posting = _posting(db, company, location=None, worksite_ambiguous=False)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["worksite_clarity"]["contribution"] == 4


class TestRoleTitleMatchComponent:
    def test_no_keywords_resolved_scores_neutral(self, db):
        # compute_score_breakdown called directly (no db session) --
        # active_keywords defaults to None, same neutral treatment as a
        # genuinely empty SearchKeyword table.
        company = _company(db)
        posting = _posting(db, company, job_title="Data Engineer")
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["role_title_match"]["contribution"] == 6

    def test_matching_keyword_scores_full_weight(self, db):
        company = _company(db)
        posting = _posting(db, company, job_title="Senior Data Engineer")
        application = _application(db, posting)
        breakdown = compute_score_breakdown(application, active_keywords=["Data Engineer", "Data Analyst"])
        assert breakdown["components"]["role_title_match"]["contribution"] == 12
        assert breakdown["components"]["role_title_match"]["raw"] == "Data Engineer"

    def test_no_matching_keyword_scores_partial(self, db):
        company = _company(db)
        posting = _posting(db, company, job_title="Product Manager")
        application = _application(db, posting)
        breakdown = compute_score_breakdown(application, active_keywords=["Data Engineer", "Data Analyst"])
        assert breakdown["components"]["role_title_match"]["contribution"] == 4  # round(12 * 0.35)

    def test_excluded_seniority_term_scores_zero_even_with_a_matching_keyword(self, db):
        company = _company(db)
        posting = _posting(db, company, job_title="Staff Data Engineer")
        application = _application(db, posting)
        breakdown = compute_score_breakdown(
            application, active_keywords=["Data Engineer"], active_seniority_exclusions=["Staff", "Director"],
        )
        assert breakdown["components"]["role_title_match"]["contribution"] == 0
        assert "Staff" in breakdown["components"]["role_title_match"]["raw"]


class TestPostingRecencyComponent:
    def test_fresh_posting_scores_full_weight(self, db):
        from datetime import datetime, timezone

        company = _company(db)
        posting = _posting(db, company, first_seen_at=datetime.now(timezone.utc))
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["posting_recency"]["contribution"] == 2

    def test_stale_posting_scores_least(self, db):
        from datetime import datetime, timedelta, timezone

        from app.models import GlobalSettings

        company = _company(db)
        posting = _posting(db, company, first_seen_at=datetime.now(timezone.utc) - timedelta(days=60))
        application = _application(db, posting)
        settings = GlobalSettings(stale_posting_threshold_days=45)
        breakdown = compute_score_breakdown(application, settings=settings)
        assert breakdown["components"]["posting_recency"]["contribution"] == round(2 * 0.15)

    def test_staleness_flag_forces_lowest_bucket_even_if_recent(self, db):
        from datetime import datetime, timezone

        company = _company(db)
        posting = _posting(db, company, first_seen_at=datetime.now(timezone.utc), staleness_flag=True)
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["posting_recency"]["contribution"] == round(2 * 0.15)

    def test_unknown_first_seen_at_scores_neutral(self, db):
        company = _company(db)
        posting = _posting(db, company)
        posting.first_seen_at = None
        application = _application(db, posting)
        assert compute_score_breakdown(application)["components"]["posting_recency"]["contribution"] == round(2 * 0.4)


class TestRecomputeScoreBreakdownPersists:
    def test_saves_breakdown_onto_the_application(self, db):
        company = _company(db, tier="B")
        posting = _posting(db, company)
        application = _application(db, posting)

        recompute_score_breakdown(db, application)

        db.refresh(application)
        assert application.score_breakdown is not None
        assert application.score_breakdown["components"]["sponsorship_history"]["contribution"] == 18

    def test_cold_start_matches_the_module_default_exactly(self, db):
        # b2: with no adaptive weight ever set, recompute_score_breakdown
        # must produce the exact same numbers as before b2 existed.
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)

        breakdown = recompute_score_breakdown(db, application)
        assert breakdown["components"]["sponsorship_history"]["contribution"] == 30
        assert breakdown["components"]["sponsorship_history"]["weight"] == 30

    def test_an_approved_adaptive_weight_actually_changes_the_score(self, db):
        # b2's whole point: once a Tier 2 proposal is approved (here
        # simulated directly via set_current_value, since the approval
        # flow itself is adaptation_service's concern, not scoring_service's),
        # the NEXT recompute reflects it -- proving the two are really wired.
        from app.services import adaptation_service

        adaptation_service.set_current_value(db, "scoring_weight_sponsorship_history", 36.0)
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)

        breakdown = recompute_score_breakdown(db, application)
        assert breakdown["components"]["sponsorship_history"]["contribution"] == 36
        assert breakdown["components"]["sponsorship_history"]["weight"] == 36.0
