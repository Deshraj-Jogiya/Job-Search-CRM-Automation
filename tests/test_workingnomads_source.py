from unittest.mock import patch

from app.services.sources import workingnomads_source
from app.services.sources.base import RawPosting


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


class TestIsConfigured:
    def test_always_true(self):
        assert workingnomads_source.is_configured() is True


class TestCheapScan:
    def test_returns_matching_posting(self, db):
        resp = _fake_response(
            json_data=[
                {
                    "url": "https://www.workingnomads.com/job/go/1850911/",
                    "title": "Data Engineer",
                    "description": "<p>Build pipelines.</p>",
                    "company_name": "Acme Corp",
                    "category_name": "Development",
                    "tags": "python,sql",
                    "location": "Worldwide",
                    "pub_date": "2026-09-10T17:17:28-04:00",
                },
            ]
        )
        with patch.object(workingnomads_source.requests, "get", return_value=resp):
            postings = workingnomads_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.job_title == "Data Engineer"
        assert posting.company_name_raw == "Acme Corp"
        assert posting.external_id == "https://www.workingnomads.com/job/go/1850911/"
        assert posting.job_description.strip() == "Build pipelines."
        assert posting.posted_at is not None
        assert posting.location == "Worldwide"

    def test_filters_out_non_matching_titles(self, db):
        resp = _fake_response(
            json_data=[
                {"url": "https://x", "title": "Sales Manager", "company_name": "Acme", "description": "d"},
            ]
        )
        with patch.object(workingnomads_source.requests, "get", return_value=resp):
            postings = workingnomads_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_skips_entries_missing_title_or_url(self, db):
        resp = _fake_response(json_data=[{"title": "", "url": "https://x", "company_name": "Acme"}])
        with patch.object(workingnomads_source.requests, "get", return_value=resp):
            postings = workingnomads_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_network_failure_returns_empty(self, db):
        with patch.object(workingnomads_source.requests, "get", side_effect=Exception("boom")):
            assert workingnomads_source.cheap_scan(["Data Engineer"], "United States") == []

    def test_respects_the_limit(self, db):
        jobs = [
            {
                "url": f"https://x/{i}", "title": "Data Engineer", "company_name": "Acme",
                "description": "d", "location": "Worldwide",
            }
            for i in range(5)
        ]
        resp = _fake_response(json_data=jobs)
        with patch.object(workingnomads_source.requests, "get", return_value=resp):
            postings = workingnomads_source.cheap_scan(["Data Engineer"], "United States", limit=2)
        assert len(postings) == 2


class TestFetchFullDescription:
    def test_returns_already_fetched_description(self):
        posting = RawPosting(
            source="workingnomads", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://www.workingnomads.com/job/go/1/", job_description="Already here.",
        )
        assert workingnomads_source.fetch_full_description(posting) == "Already here."
