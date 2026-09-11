from unittest.mock import patch

from app.models import Company
from app.services.sources import workable_source


def _fake_response(status_code=200, json_data=None):
    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

        def json(self):
            return json_data

    return _Resp()


def _make_company(db, name="Acme Corp", slug="acme"):
    from app.services.company_utils import normalize_company_name

    company = Company(name=name, normalized_name=normalize_company_name(name), workable_slug=slug)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


class TestIsConfigured:
    def test_false_with_no_tracked_companies(self, db):
        assert workable_source.is_configured() is False

    def test_true_once_a_company_has_a_slug(self, db):
        _make_company(db)
        assert workable_source.is_configured() is True


class TestCheapScan:
    def test_returns_matching_posting_with_full_description_inline(self, db):
        _make_company(db, name="Acme Corp", slug="acme")
        resp = _fake_response(
            json_data={
                "name": "Acme Corp",
                "jobs": [
                    {
                        "title": "Data Engineer",
                        "shortcode": "ABC123",
                        "shortlink": "https://apply.workable.com/j/ABC123",
                        "city": "New York",
                        "state": "NY",
                        "country": "United States",
                        "description": "<p>Build pipelines.</p>",
                    }
                ],
            }
        )
        with patch.object(workable_source.requests, "get", return_value=resp):
            postings = workable_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.job_title == "Data Engineer"
        assert posting.external_id == "ABC123"
        assert posting.job_description.strip() == "Build pipelines."
        assert posting.location == "New York, NY, United States"
        assert posting.job_url == "https://apply.workable.com/j/ABC123"

    def test_filters_out_non_matching_titles(self, db):
        _make_company(db)
        resp = _fake_response(json_data={"name": "Acme Corp", "jobs": [{"title": "Sales Manager", "shortcode": "X"}]})
        with patch.object(workable_source.requests, "get", return_value=resp):
            postings = workable_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_no_tracked_companies_returns_empty(self, db):
        assert workable_source.cheap_scan(["Data Engineer"], "United States") == []

    def test_network_failure_for_one_company_does_not_crash(self, db):
        _make_company(db)
        with patch.object(workable_source.requests, "get", side_effect=Exception("boom")):
            assert workable_source.cheap_scan(["Data Engineer"], "United States") == []


class TestFetchFullDescription:
    def test_returns_the_already_fetched_description(self, db):
        from app.services.sources.base import RawPosting

        posting = RawPosting(
            source="workable", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://apply.workable.com/j/ABC123", job_description="Already here.",
        )
        assert workable_source.fetch_full_description(posting) == "Already here."
