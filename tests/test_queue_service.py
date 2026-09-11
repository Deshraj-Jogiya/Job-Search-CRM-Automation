import json

import pytest

from app.database import utcnow
from app.models import Company, JobApplication, JobPosting
from app.services import queue_service
from app.services.company_utils import normalize_company_name


def _company(db, **overrides):
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


class TestBuildQueueTabs:
    def test_cap_exempt_company_goes_in_cap_exempt_tab(self, db):
        company = _company(db, is_cap_exempt=True, tier="C")
        posting = _posting(db, company)
        _application(db, posting)

        result = queue_service.build_queue(db)
        assert len(result["cap_exempt"]) == 1
        assert len(result["tier_ab"]) == 0

    def test_tier_a_non_cap_exempt_goes_in_tier_ab_tab(self, db):
        company = _company(db, is_cap_exempt=False, tier="A")
        posting = _posting(db, company)
        _application(db, posting)

        result = queue_service.build_queue(db)
        assert len(result["tier_ab"]) == 1
        assert len(result["cap_exempt"]) == 0

    def test_tier_c_goes_in_everything_else(self, db):
        company = _company(db, is_cap_exempt=False, tier="C")
        posting = _posting(db, company)
        _application(db, posting)

        result = queue_service.build_queue(db)
        assert len(result["everything_else"]) == 1

    def test_no_company_goes_in_everything_else(self, db):
        posting = JobPosting(company_id=None, company_name_raw="Unknown", job_title="X", job_description="d", source="manual")
        db.add(posting)
        db.commit()
        db.refresh(posting)
        _application(db, posting)

        result = queue_service.build_queue(db)
        assert len(result["everything_else"]) == 1


class TestHardGateExclusion:
    def test_sponsorship_blocked_posting_excluded_from_every_tab(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company, sponsorship_blocked=True)
        _application(db, posting)

        result = queue_service.build_queue(db)
        total = len(result["cap_exempt"]) + len(result["tier_ab"]) + len(result["everything_else"])
        assert total == 0

    def test_terminal_status_excluded(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        _application(db, posting, status="Applied")

        result = queue_service.build_queue(db)
        total = len(result["cap_exempt"]) + len(result["tier_ab"]) + len(result["everything_else"])
        assert total == 0

    def test_skipped_excluded(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        _application(db, posting, skipped_at=utcnow(), skip_reason="not_interested")

        result = queue_service.build_queue(db)
        total = len(result["cap_exempt"]) + len(result["tier_ab"]) + len(result["everything_else"])
        assert total == 0


class TestSortedByScore:
    def test_higher_score_sorts_first(self, db):
        company = _company(db, tier="A")
        posting_low = _posting(db, company, job_title="Low Score Role")
        posting_high = _posting(db, company, job_title="High Score Role")
        _application(db, posting_low, score_breakdown={"total": 20})
        _application(db, posting_high, score_breakdown={"total": 90})

        result = queue_service.build_queue(db)
        titles = [a.posting.job_title for a in result["tier_ab"]]
        assert titles == ["High Score Role", "Low Score Role"]

    def test_falls_back_to_match_score_when_no_breakdown_yet(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting, match_score=55, score_breakdown=None)
        assert queue_service._score_of(application) == 55


class TestSkipApplication:
    def test_skip_sets_reason_and_timestamp(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)

        queue_service.skip_application(db, application.id, "not_interested")

        db.refresh(application)
        assert application.skipped_at is not None
        assert application.skip_reason == "not_interested"
        assert application.status == "Ingested"  # unchanged, unlike mark_not_a_fit

    def test_invalid_reason_rejected(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)

        with pytest.raises(queue_service.QueueServiceError):
            queue_service.skip_application(db, application.id, "made_up_reason")

    def test_unskip_reverses_it(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)
        queue_service.skip_application(db, application.id, "bad_timing")

        queue_service.unskip_application(db, application.id)

        db.refresh(application)
        assert application.skipped_at is None
        assert application.skip_reason is None


class TestMarkNotAFit:
    def test_sets_rejected_status(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting)

        queue_service.mark_not_a_fit(db, application.id, "low_priority")

        db.refresh(application)
        assert application.status == "Rejected"
        assert application.rejected_at is not None
        assert application.skip_reason == "low_priority"

    def test_works_from_ingested_unlike_confirmation_service_reject(self, db):
        # confirmation_service.reject_application requires Pending
        # Confirmation/Needs Review -- mark_not_a_fit must work earlier
        # in the lifecycle, since /queue triages before that stage.
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting, status="Ingested")

        queue_service.mark_not_a_fit(db, application.id, "duplicate_role")
        db.refresh(application)
        assert application.status == "Rejected"

    def test_already_terminal_raises(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        application = _application(db, posting, status="Applied")

        with pytest.raises(queue_service.QueueServiceError):
            queue_service.mark_not_a_fit(db, application.id, "low_priority")


class TestRecomputeAllTiers:
    def test_idempotent_and_recomputes_from_scratch(self, db, settings):
        company = _company(db, tier="A", lca_filings_total=0, h1b_approvals_total=0, is_cap_exempt=False)
        queue_service.recompute_all_tiers(db)
        db.refresh(company)
        assert company.tier == "X"  # corrected down from a stale "A"
        assert company.tier_computed_at is not None
