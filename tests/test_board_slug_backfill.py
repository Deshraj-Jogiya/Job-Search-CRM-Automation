"""Covers intake_service._apply_discovered_slugs's b1.2 wiring: the
winning slug form (no_space/hyphenated) for each ATS platform gets
logged via adaptation_service.record_slug_strategy_hit, so
_backfill_board_slugs's next run can try the historically-better guess
first (see board_discovery.discover_slugs_with_forms)."""

from app.models import AdaptationLog, Company
from app.services.company_utils import normalize_company_name
from app.services.intake_service import _apply_discovered_slugs


def _company(db, name="Acme Corp"):
    company = Company(name=name, normalized_name=normalize_company_name(name))
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


def test_applies_slug_and_logs_the_winning_form(db):
    company = _company(db)
    results = {
        "greenhouse": ("acme-corp", "hyphenated"),
        "lever": (None, None),
        "ashby": (None, None),
        "recruitee": (None, None),
        "personio": (None, None),
        "workable": (None, None),
        "smartrecruiters": (None, None),
    }

    _apply_discovered_slugs(db, company, results)

    assert company.greenhouse_slug == "acme-corp"
    entry = db.query(AdaptationLog).filter(AdaptationLog.subsystem == "ats_slug_strategy").one()
    assert entry.parameter == "greenhouse:hyphenated"


def test_no_hits_logs_nothing(db):
    company = _company(db)
    results = {ats: (None, None) for ats in
               ("greenhouse", "lever", "ashby", "recruitee", "personio", "workable", "smartrecruiters")}

    _apply_discovered_slugs(db, company, results)

    assert db.query(AdaptationLog).filter(AdaptationLog.subsystem == "ats_slug_strategy").count() == 0


def test_multiple_platform_hits_each_logged(db):
    company = _company(db)
    results = {
        "greenhouse": ("acmecorp", "no_space"),
        "lever": ("acme-corp", "hyphenated"),
        "ashby": (None, None),
        "recruitee": (None, None),
        "personio": (None, None),
        "workable": (None, None),
        "smartrecruiters": (None, None),
    }

    _apply_discovered_slugs(db, company, results)

    logged = {e.parameter for e in db.query(AdaptationLog).filter(AdaptationLog.subsystem == "ats_slug_strategy")}
    assert logged == {"greenhouse:no_space", "lever:hyphenated"}
