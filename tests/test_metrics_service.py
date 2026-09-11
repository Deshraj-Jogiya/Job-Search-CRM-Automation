from datetime import timedelta

from app.database import utcnow
from app.models import Company, JobApplication, JobPosting, ProfileVariant
from app.services import metrics_service
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


def _application(db, company, posting_overrides=None, **overrides):
    """posting_id is unique per JobApplication (one-to-one) -- always
    creates a fresh JobPosting under the hood so callers can create
    several applications for the same company without a unique-
    constraint collision."""
    posting = _posting(db, company, **(posting_overrides or {}))
    defaults = dict(posting_id=posting.id)
    defaults.update(overrides)
    application = JobApplication(**defaults)
    db.add(application)
    db.commit()
    db.refresh(application)
    return application


class TestWeeklyFunnel:
    def test_counts_and_rates(self, db):
        company = _company(db)
        now = utcnow()
        _application(db, company, applied_at=now, replied_at=now)
        _application(db, company, applied_at=now, replied_at=None)

        funnel = metrics_service.weekly_funnel(db)
        assert len(funnel) == 1
        assert funnel[0]["sent"] == 2
        assert funnel[0]["replied"] == 1
        assert funnel[0]["reply_rate"] == 50.0

    def test_ignores_unapplied_applications(self, db):
        company = _company(db)
        _application(db, company, applied_at=None)

        assert metrics_service.weekly_funnel(db) == []

    def test_buckets_into_separate_weeks(self, db):
        company = _company(db)
        now = utcnow()
        _application(db, company, applied_at=now)
        _application(db, company, applied_at=now - timedelta(days=14))

        funnel = metrics_service.weekly_funnel(db)
        assert len(funnel) == 2


class TestMedianDaysToFirstReply:
    def test_computes_median(self, db):
        company = _company(db)
        applied = utcnow() - timedelta(days=10)
        _application(db, company, applied_at=applied, replied_at=applied + timedelta(days=2))
        _application(db, company, applied_at=applied, replied_at=applied + timedelta(days=4))

        result = metrics_service.median_days_to_first_reply(db)
        assert result["count"] == 2
        assert result["median_days"] == 3.0

    def test_no_replies_yet(self, db):
        company = _company(db)
        _application(db, company, applied_at=utcnow())

        result = metrics_service.median_days_to_first_reply(db)
        assert result == {"count": 0, "median_days": None}


class TestSegmentation:
    def test_by_resume_version(self, db):
        variant = ProfileVariant(name="Data Engineering", is_default=True)
        db.add(variant)
        db.commit()
        db.refresh(variant)

        company = _company(db)
        now = utcnow()
        _application(db, company, applied_at=now, replied_at=now, profile_variant_id=variant.id)
        _application(db, company, applied_at=now, profile_variant_id=None)

        segments = {s["segment"]: s for s in metrics_service.by_resume_version(db)}
        assert segments["Data Engineering"]["sent"] == 1
        assert segments["Data Engineering"]["replied"] == 1
        assert segments["(unassigned)"]["sent"] == 1

    def test_by_company_tier(self, db):
        company_a = _company(db, name="Tier A Co", normalized_name="tier a co", tier="A")
        _application(db, company_a, applied_at=utcnow())

        segments = {s["segment"]: s for s in metrics_service.by_company_tier(db)}
        assert segments["A"]["sent"] == 1

    def test_by_cap_exempt(self, db):
        company = _company(db, is_cap_exempt=True)
        _application(db, company, applied_at=utcnow())

        segments = {s["segment"]: s for s in metrics_service.by_cap_exempt(db)}
        assert segments["Cap-exempt"]["sent"] == 1

    def test_by_wage_level(self, db):
        company = _company(db, max_wage_level_15xx="IV")
        _application(db, company, applied_at=utcnow())

        segments = {s["segment"]: s for s in metrics_service.by_wage_level(db)}
        assert segments["IV"]["sent"] == 1

    def test_by_source(self, db):
        company = _company(db)
        _application(db, company, posting_overrides={"source": "lever"}, applied_at=utcnow())

        segments = {s["segment"]: s for s in metrics_service.by_source(db)}
        assert segments["lever"]["sent"] == 1


class TestUnderperformingResumeVersions:
    def test_flags_a_version_below_half_the_best(self, db):
        strong = ProfileVariant(name="Strong", is_default=True)
        weak = ProfileVariant(name="Weak", is_default=False)
        db.add_all([strong, weak])
        db.commit()
        db.refresh(strong)
        db.refresh(weak)

        company = _company(db)
        now = utcnow()
        # Strong: 4/4 replied = 100%
        for _ in range(4):
            _application(db, company, applied_at=now, replied_at=now, profile_variant_id=strong.id)
        # Weak: 1/4 replied = 25% (< 50% of 100%)
        _application(db, company, applied_at=now, replied_at=now, profile_variant_id=weak.id)
        for _ in range(3):
            _application(db, company, applied_at=now, replied_at=None, profile_variant_id=weak.id)

        flagged = {s["segment"] for s in metrics_service.underperforming_resume_versions(db)}
        assert "Weak" in flagged
        assert "Strong" not in flagged

    def test_ignores_small_samples(self, db):
        strong = ProfileVariant(name="Strong", is_default=True)
        weak = ProfileVariant(name="Weak", is_default=False)
        db.add_all([strong, weak])
        db.commit()
        db.refresh(strong)
        db.refresh(weak)

        company = _company(db)
        now = utcnow()
        for _ in range(4):
            _application(db, company, applied_at=now, replied_at=now, profile_variant_id=strong.id)
        # Only 1 send for weak -- below the minimum sample size, shouldn't be flagged
        _application(db, company, applied_at=now, replied_at=None, profile_variant_id=weak.id)

        flagged = {s["segment"] for s in metrics_service.underperforming_resume_versions(db)}
        assert "Weak" not in flagged
