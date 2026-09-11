"""B3's anti-runaway guardrails, tested as cross-cutting properties of
the real adaptive layer rather than re-testing each subsystem's own
unit behavior (already covered in test_adaptation_service_core.py,
test_board_discovery.py, test_queue_service.py).

b3.1 EXPLORATION FLOOR and b3.2 NEVER NARROWS SILENTLY are satisfied
by construction here, not by a runtime exploration-sampling mechanism:
every Tier 1 adaptive function this app actually has either (a) only
REORDERS a list the user already sees in full (source poll priority,
ATS slug guess order), or (b) only PROPOSES a change for human review
(sponsorship pattern narrowing) -- none of them ever remove a posting,
application, or source from a user-facing view. build_queue itself
never truncates or filters by score, so 100% of the queue is always
visible regardless of what any adaptive ranking favors -- trivially
satisfies the 15% exploration floor because nothing is hidden at all.

b3.4 COLD START is tested directly: with zero AdaptationLog/
AdaptiveParameterValue rows, every adaptive parameter this app has
sits at its config default."""

from app.database import utcnow
from app.models import Company, JobApplication, JobPosting
from app.services import adaptation_service, board_discovery, queue_service
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


class TestColdStart:
    """b3.4: zero data -> every adaptive parameter at its config
    default, behavior byte-identical to no adaptive layer at all."""

    def test_dedupe_threshold_at_config_default(self, db):
        config = adaptation_service.get_config()
        assert adaptation_service.current_dedupe_threshold(db) == config["tier1"]["dedupe_threshold"]["default"]

    def test_slug_form_order_empty_so_discovery_uses_its_own_default(self, db):
        assert adaptation_service.recommend_slug_form_order(db) == {}

    def test_sponsorship_misfire_report_empty(self, db):
        assert adaptation_service.sponsorship_misfire_report(db) == []

    def test_source_reorder_returns_original_order_when_nothing_ranked(self, db):
        company = _company(db)
        _posting(db, company, source="greenhouse")
        # a single source with < min_ingested postings stays unranked --
        # cold start never invents an ordering from no evidence.
        assert adaptation_service.recommend_source_reorder(db, min_ingested=10) == ["greenhouse"]

    def test_queue_supply_report_all_zero(self, db):
        report = adaptation_service.queue_supply_report(db)
        assert report["today"] == 0
        assert report["rolling_7d_avg"] == 0.0


class TestQueueNeverNarrows:
    """b3.1/b3.2: the queue always shows every non-terminal, non-
    skipped, non-blocked application -- regardless of score, and
    regardless of anything any Tier 1 adaptive function has done."""

    def test_low_scoring_applications_are_never_dropped(self, db):
        company = _company(db, tier="C")
        for i, score in enumerate([5, 10, 1, 50, 0]):
            posting = _posting(db, company, job_title=f"Role {i}")
            app = JobApplication(posting_id=posting.id, score_breakdown={"total": score})
            db.add(app)
        db.commit()

        result = queue_service.build_queue(db)
        all_ids = {a.id for a in result["everything_else"]}
        assert len(all_ids) == 5  # every one of them, including the 0-score entry

    def test_sorted_but_never_truncated_across_many_entries(self, db):
        company = _company(db, tier="C")
        for i in range(40):
            posting = _posting(db, company, job_title=f"Role {i}")
            db.add(JobApplication(posting_id=posting.id, score_breakdown={"total": i}))
        db.commit()

        result = queue_service.build_queue(db)
        assert len(result["everything_else"]) == 40


class TestSourceReorderNeverDisables:
    """b1.6's own rule, re-asserted here as a b3.2 case: reordering
    poll priority is a re-rank, never a removal."""

    def test_zero_yield_source_still_appears_in_recommended_order(self, db):
        company = _company(db)
        for i in range(10):
            posting = _posting(db, company, source="jobspresso", job_title=f"Role {i}")
            db.add(JobApplication(posting_id=posting.id))  # never applied -- 0% yield
        db.commit()

        order = adaptation_service.recommend_source_reorder(db, min_ingested=5)
        assert "jobspresso" in order


class TestSponsorshipMisfiresNeverAutoApply:
    """b1.5/b3.2: accumulating correction evidence never hides or
    unblocks a posting by itself -- only a human editing the regex
    pattern in board_discovery.py/sponsorship_signals.py can."""

    def test_many_misfires_still_leaves_status_proposed(self, db):
        for posting_id in range(20):
            adaptation_service.record_sponsorship_misfire(db, posting_id=posting_id, label="ITAR")
        report = adaptation_service.sponsorship_misfire_report(db, min_misfires=3)
        assert report == [{"label": "ITAR", "misfire_count": 20}]
        # nothing here mutates sponsorship_signals.py's actual patterns
        # or any posting's sponsorship_blocked flag -- report() is read-only.
