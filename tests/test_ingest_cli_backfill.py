from app.ingest.cli import backfill_signals
from app.models import Company, JobApplication, JobPosting
from app.services.company_utils import normalize_company_name


def test_backfill_signals_is_idempotent_and_updates_everything(db):
    company = Company(
        name="Acme Corp", normalized_name=normalize_company_name("Acme Corp"),
        lca_filings_total=10, max_wage_level_15xx="IV", tier=None,  # stale/never-computed tier
    )
    db.add(company)
    db.commit()
    db.refresh(company)

    posting = JobPosting(
        company_id=company.id, company_name_raw="Acme Corp", job_title="Data Engineer",
        job_description="We will sponsor H-1B candidates. Based in Austin, TX.",
        source="greenhouse",
        # Simulates a posting ingested before sponsorship_signals.py existed
        sponsorship_blocked=False, sponsorship_signal=False, worksite_ambiguous=False, signal_matches=None,
    )
    db.add(posting)
    db.commit()
    db.refresh(posting)

    application = JobApplication(posting_id=posting.id)
    db.add(application)
    db.commit()
    db.refresh(application)

    result = backfill_signals(db)

    db.refresh(company)
    db.refresh(posting)
    db.refresh(application)

    assert company.tier == "A"
    assert posting.sponsorship_signal is True
    assert posting.signal_matches["signal"] == ["H-1B mentioned", "will sponsor"] or "H-1B mentioned" in posting.signal_matches["signal"]
    assert application.score_breakdown is not None
    assert application.score_breakdown["components"]["sponsorship_history"]["contribution"] == 30

    assert result["postings_signal_updated"] == 1
    assert result["companies_total"] == 1
    assert result["applications_total"] == 1

    # Rerun -- idempotent: the first run already moved tier from None to
    # "A" (companies_changed=1 above), so a second run settles to a
    # stable end state (companies_changed=0) rather than flip-flopping.
    result2 = backfill_signals(db)
    assert result2["companies_changed"] == 0
    assert result2["postings_signal_updated"] == 1
    assert result2["applications_total"] == 1
    db.refresh(company)
    assert company.tier == "A"
