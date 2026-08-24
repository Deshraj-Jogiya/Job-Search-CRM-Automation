"""Analytics math. `by_score_band` and `by_source`'s average-score
calculation both had a real bug once: match_score's column default is
0, not NULL, so a never-scored application looks identical to a
genuine 0-59% match unless the query also filters on
match_analysis_json (the field that's only set once scoring has
actually run). These tests seed that exact never-scored-vs-genuinely-
low-scored mix and assert it stays excluded.
"""

from app.database import utcnow

from conftest import make_application, make_company, make_posting

from app.services import analytics_service


def test_rate_returns_none_for_a_zero_denominator_instead_of_dividing_by_zero():
    assert analytics_service._rate(5, 0) is None


def test_rate_computes_a_rounded_percentage():
    assert analytics_service._rate(1, 3) == 33.3


def test_by_score_band_excludes_never_scored_applications(db):
    company = make_company(db)
    posting_a = make_posting(db, company, source="greenhouse")
    posting_b = make_posting(db, company, source="greenhouse", job_url="https://example.com/job/2")

    # Never scored: match_score sits at its column default (0), but
    # match_analysis_json was never set -- must NOT land in the 0-59 band.
    make_application(db, posting_a, status="Ingested", match_score=0, match_analysis_json=None)
    # Genuinely scored low: this one belongs in 0-59.
    make_application(db, posting_b, status="Tailored", match_score=45, match_analysis_json='{"scored": true}')

    bands = {b["band"]: b for b in analytics_service.by_score_band(db)}

    assert bands["0-59"]["total"] == 1


def test_by_source_average_score_excludes_never_scored_applications(db):
    company = make_company(db)
    posting_a = make_posting(db, company, source="adzuna")
    posting_b = make_posting(db, company, source="adzuna", job_url="https://example.com/job/2")

    make_application(db, posting_a, match_score=0, match_analysis_json=None)
    make_application(db, posting_b, match_score=80, match_analysis_json='{"scored": true}')

    by_source = {r["source"]: r for r in analytics_service.by_source(db)}

    assert by_source["adzuna"]["avg_match_score"] == 80.0


def test_conversion_rates_use_the_dedicated_timestamps_not_current_status(db):
    company = make_company(db)
    posting = make_posting(db, company, source="greenhouse")
    # Went straight from Applied to Offer -- status is now "Offer", but
    # interviewing_at was never set. Should still count as an offer.
    make_application(
        db, posting, status="Offer", applied_at=utcnow(), offer_at=utcnow(), interviewing_at=None
    )

    rates = analytics_service.conversion_rates(db)

    assert rates["offers"] == 1
    assert rates["interviewed"] == 0
