"""_find_matching_posting: fuzzy-title dedup/repost detection used by
every direct-ATS intake cycle. Real, significant egress bug found live
2026-09-18: the candidate-scanning loop used to fetch full JobPosting
ORM objects (every column, including job_description -- often several
KB of text never actually read here) for every historical posting a
company has ever had, on every raw posting scanned, every intake cycle.
Narrowed to the 3 columns actually used; these tests lock in that the
narrowing didn't change real matching/repost behavior."""

from datetime import timedelta

from conftest import make_company, make_posting

from app.database import utcnow
from app.services.intake_service import _find_matching_posting, _flag_stale_postings
from app.services.sources.base import RawPosting


def _raw(job_title, source="greenhouse", job_url="https://example.com/job/new", external_id=None):
    return RawPosting(
        source=source,
        company_name_raw="Acme Corp",
        job_title=job_title,
        job_url=job_url,
        external_id=external_id,
        job_description="A real job description.",
    )


def test_fuzzy_title_match_finds_an_existing_posting_and_flags_it_as_not_a_repost(db):
    company = make_company(db)
    make_posting(db, company, job_title="Data Engineer", last_seen_at=utcnow())

    matched, is_repost = _find_matching_posting(db, company.id, _raw("Data Engineer II"))

    assert matched is not None
    assert matched.job_title == "Data Engineer"
    assert is_repost is False


def test_a_posting_not_seen_in_a_long_time_is_flagged_as_a_repost(db):
    company = make_company(db)
    make_posting(db, company, job_title="Data Engineer", last_seen_at=utcnow() - timedelta(days=60))

    matched, is_repost = _find_matching_posting(db, company.id, _raw("Data Engineer"))

    assert matched is not None
    assert is_repost is True


def test_no_match_returns_none(db):
    company = make_company(db)
    make_posting(db, company, job_title="Marketing Manager", last_seen_at=utcnow())

    matched, is_repost = _find_matching_posting(db, company.id, _raw("Data Engineer"))

    assert matched is None
    assert is_repost is False


def test_exact_external_id_match_wins_over_fuzzy_title_matching(db):
    company = make_company(db)
    existing = make_posting(db, company, job_title="Data Engineer", external_id="12345", last_seen_at=utcnow())

    matched, is_repost = _find_matching_posting(db, company.id, _raw("Completely Different Title", external_id="12345"))

    assert matched is not None
    assert matched.id == existing.id
    assert is_repost is False


def test_flag_stale_postings_flags_old_unflagged_postings_via_bulk_update(db):
    """Real gap found live 2026-09-18: this used to fetch full ORM
    objects (every column, including job_description) just to flip one
    boolean, and ran on EVERY scheduler tick with no cadence gate at
    all. Switched to a single bulk UPDATE that never fetches row data;
    this locks in the external behavior is unchanged."""
    company = make_company(db)
    old_posting = make_posting(
        db, company, job_url="https://example.com/job/old", first_seen_at=utcnow() - timedelta(days=60)
    )
    fresh_posting = make_posting(
        db, company, job_url="https://example.com/job/fresh", first_seen_at=utcnow() - timedelta(days=1)
    )
    already_flagged = make_posting(
        db, company, job_url="https://example.com/job/already-flagged",
        first_seen_at=utcnow() - timedelta(days=90), staleness_flag=True,
    )

    _flag_stale_postings(db, threshold_days=45)

    db.refresh(old_posting)
    db.refresh(fresh_posting)
    db.refresh(already_flagged)
    assert old_posting.staleness_flag is True
    assert fresh_posting.staleness_flag is False
    assert already_flagged.staleness_flag is True
