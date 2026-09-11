"""B2: the generic Tier 2 evidence engine (evaluate_comparison) and
its approve/reject actions. Covers the NOT_REPORTABLE/REPORTABLE/
CONFOUNDED state machine, the confidence interval, confound detection
(b3.5), and the bounded proposed_adjustment for tier/wage_level
comparisons (resume_variant/source stay informational-only -- no
natural expected ordering to auto-propose against)."""

from app.database import utcnow
from app.models import AdaptationLog, Company, JobApplication, JobPosting
from app.services import adaptation_service
from app.services.company_utils import normalize_company_name

_company_counter = [0]


def _company(db, **overrides):
    _company_counter[0] += 1
    name = overrides.pop("name", f"Company {_company_counter[0]}")
    defaults = dict(name=name, normalized_name=normalize_company_name(name))
    defaults.update(overrides)
    company = Company(**defaults)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def _applied_application(db, company, replied, source="greenhouse", title="Data Engineer"):
    posting = JobPosting(
        company_id=company.id, company_name_raw=company.name, job_title=title,
        job_description="d", source=source,
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)
    app = JobApplication(
        posting_id=posting.id, applied_at=utcnow(), replied_at=(utcnow() if replied else None),
    )
    db.add(app)
    db.commit()
    return app


def _seed_tier_comparison(db, best_tier="A", worst_tier="C", best_source="greenhouse", worst_source="greenhouse"):
    """20 applied/15 replied at best_tier, 20 applied/2 replied at
    worst_tier -- 40 total sent (meets min_samples_tier_comparison=40),
    17 replied (meets min_replies=10), and a wide enough reply-rate gap
    that the 95% CI excludes zero."""
    best_company = _company(db, tier=best_tier)
    worst_company = _company(db, tier=worst_tier)
    for i in range(20):
        _applied_application(db, best_company, replied=(i < 15), source=best_source)
    for i in range(20):
        _applied_application(db, worst_company, replied=(i < 2), source=worst_source)


class TestNotReportable:
    def test_below_sample_size_stays_not_reportable(self, db):
        company = _company(db, tier="A")
        for i in range(5):
            _applied_application(db, company, replied=(i < 3))
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["state"] == "NOT_REPORTABLE"
        assert "best_segment" not in result

    def test_raw_segments_always_present_even_when_not_reportable(self, db):
        company = _company(db, tier="A")
        _applied_application(db, company, replied=True)
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert any(s["segment"] == "A" for s in result["segments"])

    def test_only_one_segment_with_data_stays_not_reportable(self, db):
        company = _company(db, tier="A")
        for i in range(50):
            _applied_application(db, company, replied=(i < 20))
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["state"] == "NOT_REPORTABLE"

    def test_enough_samples_but_too_few_replies_stays_not_reportable(self, db):
        best = _company(db, tier="A")
        worst = _company(db, tier="C")
        for _ in range(25):
            _applied_application(db, best, replied=False)
        for _ in range(25):
            _applied_application(db, worst, replied=False)
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["total_sent"] == 50
        assert result["total_replied"] == 0
        assert result["state"] == "NOT_REPORTABLE"


class TestReportable:
    def test_crosses_gates_and_produces_a_confidence_interval(self, db):
        _seed_tier_comparison(db)
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["state"] == "REPORTABLE"
        assert result["best_segment"] == "A"
        assert result["worst_segment"] == "C"
        assert result["confidence_interval"]["low"] > 0

    def test_confirming_expected_order_proposes_a_weight_increase(self, db):
        _seed_tier_comparison(db, best_tier="A", worst_tier="C")
        result = adaptation_service.evaluate_comparison(db, "tier")
        proposal = result["proposed_adjustment"]
        assert proposal is not None
        assert proposal["confirms_expected_order"] is True
        assert proposal["proposed_value"] > proposal["current_value"]

    def test_contradicting_expected_order_proposes_a_weight_decrease(self, db):
        # worst_tier wins over best_tier here -- C beats A, contradicting
        # the assumed A > C ranking.
        _seed_tier_comparison(db, best_tier="C", worst_tier="A")
        result = adaptation_service.evaluate_comparison(db, "tier")
        proposal = result["proposed_adjustment"]
        assert proposal is not None
        assert proposal["confirms_expected_order"] is False
        assert proposal["proposed_value"] < proposal["current_value"]

    def test_proposed_value_respects_the_clamp_pct(self, db):
        _seed_tier_comparison(db)
        result = adaptation_service.evaluate_comparison(db, "tier")
        proposal = result["proposed_adjustment"]
        # default weight 30, clamp_pct 20% -> max single-approval delta is 6
        assert abs(proposal["proposed_value"] - proposal["current_value"]) <= 6

    def test_resume_variant_comparison_never_proposes_a_weight_change(self, db):
        from app.models import ProfileVariant

        variant_a = ProfileVariant(name="Data Engineering")
        variant_b = ProfileVariant(name="ML Engineering")
        db.add_all([variant_a, variant_b])
        db.commit()
        company = _company(db)
        for i in range(20):
            posting = JobPosting(
                company_id=company.id, company_name_raw=company.name, job_title="DE",
                job_description="d", source="greenhouse",
            )
            db.add(posting)
            db.commit()
            db.refresh(posting)
            db.add(JobApplication(
                posting_id=posting.id, applied_at=utcnow(),
                replied_at=(utcnow() if i < 15 else None), profile_variant_id=variant_a.id,
            ))
        for i in range(20):
            posting = JobPosting(
                company_id=company.id, company_name_raw=company.name, job_title="ML",
                job_description="d", source="greenhouse",
            )
            db.add(posting)
            db.commit()
            db.refresh(posting)
            db.add(JobApplication(
                posting_id=posting.id, applied_at=utcnow(),
                replied_at=(utcnow() if i < 2 else None), profile_variant_id=variant_b.id,
            ))
        db.commit()

        result = adaptation_service.evaluate_comparison(db, "resume_variant")
        assert result["state"] == "REPORTABLE"
        assert result["proposed_adjustment"] is None


class TestConfounded:
    def test_differing_source_composition_withholds_the_proposal(self, db):
        # tier A applications all from "greenhouse", tier C all from
        # "lever" -- a real confound (source, not tier, could explain
        # the reply-rate gap).
        _seed_tier_comparison(db, best_source="greenhouse", worst_source="lever")
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["state"] == "CONFOUNDED"
        assert "proposed_adjustment" not in result

    def test_same_source_composition_is_not_confounded(self, db):
        _seed_tier_comparison(db, best_source="greenhouse", worst_source="greenhouse")
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["state"] == "REPORTABLE"


class TestApproveProposal:
    def test_approve_writes_the_adaptive_value_and_an_applied_log(self, db):
        _seed_tier_comparison(db)
        entry = adaptation_service.approve_comparison_proposal(db, "tier")
        assert entry.status == "applied"
        assert entry.tier == "tier2"
        current = adaptation_service.get_current_value(db, "scoring_weight_sponsorship_history", default=0)
        assert current == entry.new_value

    def test_approved_change_is_one_click_revertable(self, db):
        _seed_tier_comparison(db)
        entry = adaptation_service.approve_comparison_proposal(db, "tier")
        adaptation_service.revert_adaptation(db, entry.id)
        current = adaptation_service.get_current_value(db, "scoring_weight_sponsorship_history", default=0)
        assert current == entry.old_value

    def test_approve_with_no_reportable_comparison_raises(self, db):
        company = _company(db, tier="A")
        _applied_application(db, company, replied=True)
        try:
            adaptation_service.approve_comparison_proposal(db, "tier")
            assert False, "expected AdaptationServiceError"
        except adaptation_service.AdaptationServiceError:
            pass

    def test_approve_resume_variant_with_no_proposal_raises(self, db):
        from app.models import ProfileVariant

        variant_a = ProfileVariant(name="Data Engineering")
        variant_b = ProfileVariant(name="ML Engineering")
        db.add_all([variant_a, variant_b])
        db.commit()
        company = _company(db)
        for i in range(20):
            posting = JobPosting(
                company_id=company.id, company_name_raw=company.name, job_title="DE",
                job_description="d", source="greenhouse",
            )
            db.add(posting)
            db.commit()
            db.refresh(posting)
            db.add(JobApplication(
                posting_id=posting.id, applied_at=utcnow(),
                replied_at=(utcnow() if i < 15 else None), profile_variant_id=variant_a.id,
            ))
        for i in range(20):
            posting = JobPosting(
                company_id=company.id, company_name_raw=company.name, job_title="ML",
                job_description="d", source="greenhouse",
            )
            db.add(posting)
            db.commit()
            db.refresh(posting)
            db.add(JobApplication(
                posting_id=posting.id, applied_at=utcnow(),
                replied_at=(utcnow() if i < 2 else None), profile_variant_id=variant_b.id,
            ))
        db.commit()

        try:
            adaptation_service.approve_comparison_proposal(db, "resume_variant")
            assert False, "expected AdaptationServiceError"
        except adaptation_service.AdaptationServiceError:
            pass


class TestRejectProposal:
    def test_reject_logs_without_changing_current_value(self, db):
        _seed_tier_comparison(db)
        before = adaptation_service.get_current_value(db, "scoring_weight_sponsorship_history", default=30)
        entry = adaptation_service.reject_comparison_proposal(db, "tier")
        assert entry.status == "rejected"
        after = adaptation_service.get_current_value(db, "scoring_weight_sponsorship_history", default=30)
        assert before == after

    def test_reject_does_not_suppress_a_future_reevaluation(self, db):
        _seed_tier_comparison(db)
        adaptation_service.reject_comparison_proposal(db, "tier")
        result = adaptation_service.evaluate_comparison(db, "tier")
        assert result["state"] == "REPORTABLE"
        assert result["proposed_adjustment"] is not None
