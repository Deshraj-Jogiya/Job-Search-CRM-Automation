"""Boards report their publish date in different shapes; all must land as naive
UTC so a week-old job can't look new."""
from datetime import datetime, timezone

import pytest

from app.database import utcnow
from app.models import JobPosting
from app.services import intake_service
from app.services.sources.base import RawPosting, parse_posted_at


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2026-11-03T01:38:06.017+00:00", datetime(2026, 11, 3, 1, 38, 6, 17000)),  # Ashby
        ("2026-10-28T20:13:59-04:00", datetime(2026, 10, 29, 0, 13, 59)),  # Greenhouse
        (1786469891368, datetime.utcfromtimestamp(1786469891.368)),  # Lever, epoch ms
        ("2026-10-13 08:01:51 UTC", datetime(2026, 10, 13, 8, 1, 51)),  # Recruitee
        ("2026-05-29", datetime(2026, 5, 29)),  # Workable
        ("2026-11-05T06:06:39.036Z", datetime(2026, 11, 5, 6, 6, 39, 36000)),  # SmartRecruiters
    ],
)
def test_parse_posted_at_shapes(value, expected):
    assert parse_posted_at(value) == expected


@pytest.mark.parametrize("value", [None, "", "not a date"])
def test_parse_posted_at_bad_values_return_none(value):
    assert parse_posted_at(value) is None


def test_known_posting_gets_its_real_date(db):
    company = intake_service.get_or_create_company(db, "Dated Co")
    db.commit()
    posting = JobPosting(
        company_id=company.id, company_name_raw="Dated Co", job_title="Data Engineer", job_url="https://x.test/1",
        job_description="d" * 80, source="ashby", first_seen_at=utcnow(), last_seen_at=utcnow(),
    )
    db.add(posting)
    db.commit()
    real_date = datetime(2026, 10, 1)
    aware_date = real_date.replace(tzinfo=timezone.utc)  # jobspipe's parser returns aware datetimes
    raw = RawPosting(source="ashby", company_name_raw="Dated Co", job_title="Data Engineer", job_url="https://x.test/1", posted_at=aware_date)

    assert intake_service._ingest_raw_posting(db, None, raw) is None

    db.refresh(posting)
    assert posting.first_seen_at == real_date
