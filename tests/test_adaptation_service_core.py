from datetime import timedelta

import pytest

from app.database import utcnow
from app.models import AdaptationLog, Company, JobApplication, JobPosting
from app.services import adaptation_service
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
        job_description="d", source="greenhouse",
    )
    defaults.update(overrides)
    posting = JobPosting(**defaults)
    db.add(posting)
    db.commit()
    db.refresh(posting)
    return posting


class TestLogAdaptation:
    def test_writes_a_row_with_all_fields(self, db):
        entry = adaptation_service.log_adaptation(
            db, "tier1", "dedupe_threshold", "dedupe_threshold", 0.88, 0.89,
            triggering_evidence={"was_false_merge": True}, status="applied",
        )
        assert entry.id is not None
        assert entry.tier == "tier1"
        assert entry.old_value == 0.88
        assert entry.new_value == 0.89
        assert entry.status == "applied"


class TestGetSetCurrentValue:
    def test_no_row_returns_the_default_cold_start(self, db):
        assert adaptation_service.get_current_value(db, "some_param", default=0.5) == 0.5

    def test_set_then_get_returns_the_new_value(self, db):
        adaptation_service.set_current_value(db, "some_param", 0.6)
        assert adaptation_service.get_current_value(db, "some_param", default=0.5) == 0.6

    def test_set_twice_updates_in_place(self, db):
        adaptation_service.set_current_value(db, "some_param", 0.6)
        adaptation_service.set_current_value(db, "some_param", 0.7)
        assert adaptation_service.get_current_value(db, "some_param", default=0.5) == 0.7


class TestRevertAdaptation:
    def test_revert_restores_old_value(self, db):
        adaptation_service.set_current_value(db, "some_param", 0.7)
        entry = adaptation_service.log_adaptation(db, "tier1", "sub", "some_param", 0.6, 0.7)

        adaptation_service.revert_adaptation(db, entry.id)

        assert adaptation_service.get_current_value(db, "some_param", default=0.0) == 0.6
        db.refresh(entry)
        assert entry.status == "reverted"

    def test_revert_writes_a_new_log_row_not_editing_history(self, db):
        adaptation_service.set_current_value(db, "some_param", 0.7)
        entry = adaptation_service.log_adaptation(db, "tier1", "sub", "some_param", 0.6, 0.7)
        before_count = db.query(AdaptationLog).count()

        adaptation_service.revert_adaptation(db, entry.id)

        after_count = db.query(AdaptationLog).count()
        assert after_count == before_count + 1  # a NEW row, original untouched except status/reverted_at

    def test_reverting_twice_raises(self, db):
        adaptation_service.set_current_value(db, "some_param", 0.7)
        entry = adaptation_service.log_adaptation(db, "tier1", "sub", "some_param", 0.6, 0.7)
        adaptation_service.revert_adaptation(db, entry.id)

        with pytest.raises(adaptation_service.AdaptationServiceError):
            adaptation_service.revert_adaptation(db, entry.id)

    def test_revert_all_since_unwinds_multiple_changes(self, db):
        adaptation_service.set_current_value(db, "p1", 1.0)
        e1 = adaptation_service.log_adaptation(db, "tier1", "sub", "p1", 0.5, 1.0)
        adaptation_service.set_current_value(db, "p2", 2.0)
        e2 = adaptation_service.log_adaptation(db, "tier1", "sub", "p2", 1.5, 2.0)

        reverted = adaptation_service.revert_all_since(db, utcnow() - timedelta(days=1))

        assert len(reverted) == 2
        assert adaptation_service.get_current_value(db, "p1", default=0) == 0.5
        assert adaptation_service.get_current_value(db, "p2", default=0) == 1.5


class TestClampWeightChange:
    def test_within_bounds_passes_through(self):
        assert adaptation_service.clamp_weight_change(30, 32, clamp_pct=20, floor=0, ceiling=100) == 32

    def test_exceeding_clamp_pct_is_capped(self):
        # 30 * 20% = 6 -> max allowed is 36
        assert adaptation_service.clamp_weight_change(30, 50, clamp_pct=20, floor=0, ceiling=100) == 36

    def test_below_clamp_pct_is_floored(self):
        assert adaptation_service.clamp_weight_change(30, 10, clamp_pct=20, floor=0, ceiling=100) == 24

    def test_never_exceeds_hard_ceiling(self):
        assert adaptation_service.clamp_weight_change(95, 200, clamp_pct=50, floor=0, ceiling=100) == 100

    def test_never_below_hard_floor(self):
        # current=5, clamp_pct=50 -> the %-based limit (2.5) is the
        # tighter bound here, not the floor -- one approval can't jump
        # straight to 0 from 5 even though 0 is a legal floor value.
        assert adaptation_service.clamp_weight_change(5, -50, clamp_pct=50, floor=0, ceiling=100) == 2.5

    def test_floor_wins_when_it_is_the_tighter_bound(self):
        # current=1, clamp_pct=90 -> %-limit allows down to 0.1, but the
        # floor of 0 is looser here, so the %-limit still governs; use a
        # floor that's actually above the %-limit to see the floor win.
        assert adaptation_service.clamp_weight_change(10, -50, clamp_pct=90, floor=5, ceiling=100) == 5


class TestDedupeThresholdTuning:
    def test_cold_start_is_the_config_default(self, db):
        assert adaptation_service.current_dedupe_threshold(db) == 0.88

    def test_false_merge_raises_the_threshold(self, db):
        new_value = adaptation_service.record_dedupe_correction(db, was_false_merge=True)
        assert new_value == pytest.approx(0.89)

    def test_false_split_lowers_the_threshold(self, db):
        new_value = adaptation_service.record_dedupe_correction(db, was_false_merge=False)
        assert new_value == pytest.approx(0.87)

    def test_never_exceeds_config_max(self, db):
        for _ in range(20):
            new_value = adaptation_service.record_dedupe_correction(db, was_false_merge=True)
        assert new_value <= 0.95

    def test_never_below_config_min(self, db):
        for _ in range(20):
            new_value = adaptation_service.record_dedupe_correction(db, was_false_merge=False)
        assert new_value >= 0.82

    def test_each_real_change_is_logged(self, db):
        adaptation_service.record_dedupe_correction(db, was_false_merge=True)
        entries = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "dedupe_threshold").all()
        assert len(entries) == 1
        assert entries[0].tier == "tier1"


class TestSlugStrategyOrder:
    def test_cold_start_returns_empty(self, db):
        assert adaptation_service.recommend_slug_form_order(db) == {}

    def test_single_hit_puts_that_form_first(self, db):
        adaptation_service.record_slug_strategy_hit(db, "greenhouse", "hyphenated")
        assert adaptation_service.recommend_slug_form_order(db) == {"greenhouse": ["hyphenated"]}

    def test_more_frequent_form_ranks_first(self, db):
        for _ in range(3):
            adaptation_service.record_slug_strategy_hit(db, "greenhouse", "no_space")
        adaptation_service.record_slug_strategy_hit(db, "greenhouse", "hyphenated")
        assert adaptation_service.recommend_slug_form_order(db) == {"greenhouse": ["no_space", "hyphenated"]}

    def test_platforms_tracked_independently(self, db):
        adaptation_service.record_slug_strategy_hit(db, "greenhouse", "hyphenated")
        adaptation_service.record_slug_strategy_hit(db, "lever", "no_space")
        order = adaptation_service.recommend_slug_form_order(db)
        assert order == {"greenhouse": ["hyphenated"], "lever": ["no_space"]}

    def test_each_hit_is_logged_applied_not_proposed(self, db):
        adaptation_service.record_slug_strategy_hit(db, "greenhouse", "hyphenated")
        entry = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "ats_slug_strategy").one()
        assert entry.status == "applied"


class TestSponsorshipMisfireReport:
    def test_below_threshold_not_surfaced(self, db):
        adaptation_service.record_sponsorship_misfire(db, posting_id=1, label="US citizens only")
        adaptation_service.record_sponsorship_misfire(db, posting_id=2, label="US citizens only")
        assert adaptation_service.sponsorship_misfire_report(db, min_misfires=3) == []

    def test_at_threshold_is_surfaced(self, db):
        for posting_id in (1, 2, 3):
            adaptation_service.record_sponsorship_misfire(db, posting_id=posting_id, label="US citizens only")
        report = adaptation_service.sponsorship_misfire_report(db, min_misfires=3)
        assert report == [{"label": "US citizens only", "misfire_count": 3}]

    def test_never_writes_a_current_value_or_applies(self, db):
        adaptation_service.record_sponsorship_misfire(db, posting_id=1, label="ITAR")
        entry = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "sponsorship_regex").one()
        assert entry.status == "proposed"
        assert adaptation_service.get_current_value(db, "ITAR", default=None) is None

    def test_worst_offender_first(self, db):
        for posting_id in (1, 2, 3):
            adaptation_service.record_sponsorship_misfire(db, posting_id=posting_id, label="ITAR")
        for posting_id in (4, 5, 6, 7):
            adaptation_service.record_sponsorship_misfire(db, posting_id=posting_id, label="C2C")
        report = adaptation_service.sponsorship_misfire_report(db, min_misfires=3)
        assert [r["label"] for r in report] == ["C2C", "ITAR"]


class TestQueueSupplyReport:
    def test_zero_data_returns_zero_counts(self, db):
        report = adaptation_service.queue_supply_report(db)
        assert report["today"] == 0
        assert report["rolling_7d_avg"] == 0.0

    def test_counts_a_real_new_posting(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company)
        db.add(JobApplication(posting_id=posting.id))
        db.commit()

        report = adaptation_service.queue_supply_report(db)
        assert report["today"] == 1
        assert report["today_by_lane"]["tier_ab"] == 1

    def test_sponsorship_blocked_posting_excluded(self, db):
        company = _company(db, tier="A")
        posting = _posting(db, company, sponsorship_blocked=True)
        db.add(JobApplication(posting_id=posting.id))
        db.commit()

        report = adaptation_service.queue_supply_report(db)
        assert report["today"] == 0


class TestSourceYieldReport:
    def test_computes_real_funnel_counts(self, db):
        company = _company(db, tier="A")
        posting1 = _posting(db, company, source="greenhouse")
        posting2 = _posting(db, company, source="greenhouse", job_title="ML Engineer")
        db.add(JobApplication(posting_id=posting1.id, applied_at=utcnow()))
        db.add(JobApplication(posting_id=posting2.id))
        db.commit()

        report = adaptation_service.source_yield_report(db)
        greenhouse = next(r for r in report if r["source"] == "greenhouse")
        assert greenhouse["ingested"] == 2
        assert greenhouse["applied"] == 1
        assert greenhouse["yield_pct"] == 50.0

    def test_recommend_reorder_favors_higher_yield(self, db):
        company = _company(db, tier="A")
        # greenhouse: 10 ingested, 5 applied = 50% yield
        for i in range(10):
            posting = _posting(db, company, source="greenhouse", job_title=f"Role {i}")
            db.add(JobApplication(posting_id=posting.id, applied_at=utcnow() if i < 5 else None))
        # lever: 10 ingested, 1 applied = 10% yield
        for i in range(10):
            posting = _posting(db, company, source="lever", job_title=f"Lever Role {i}")
            db.add(JobApplication(posting_id=posting.id, applied_at=utcnow() if i < 1 else None))
        db.commit()

        order = adaptation_service.recommend_source_reorder(db, min_ingested=5)
        assert order.index("greenhouse") < order.index("lever")

    def test_never_disables_a_source_only_reorders(self, db):
        company = _company(db, tier="A")
        for i in range(10):
            posting = _posting(db, company, source="lever", job_title=f"Role {i}")
            db.add(JobApplication(posting_id=posting.id))  # zero applied -- 0% yield
        db.commit()

        order = adaptation_service.recommend_source_reorder(db, min_ingested=5)
        assert "lever" in order  # still present despite 0% yield


class TestTypicalTimeToCloseDays:
    def test_zero_data_returns_empty(self, db):
        assert adaptation_service.typical_time_to_close_days(db) == {}

    def test_computes_median_from_real_first_last_seen(self, db):
        company = _company(db, tier="A")
        now = utcnow()
        _posting(db, company, source="greenhouse", first_seen_at=now - timedelta(days=10), last_seen_at=now)
        result = adaptation_service.typical_time_to_close_days(db)
        assert result["greenhouse"]["median_days"] == 10
        assert result["greenhouse"]["sample_size"] == 1
