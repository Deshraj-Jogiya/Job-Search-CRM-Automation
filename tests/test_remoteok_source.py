from unittest.mock import patch

from app.services.sources import remoteok_source
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


_LEGAL_NOTICE = {"legal": "API Terms of Service: please link back..."}


class TestIsConfigured:
    def test_always_true(self):
        assert remoteok_source.is_configured() is True


class TestCheapScan:
    def test_skips_the_legal_notice_entry(self, db):
        resp = _fake_response(json_data=[_LEGAL_NOTICE])
        with patch.object(remoteok_source.requests, "get", return_value=resp):
            postings = remoteok_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_returns_matching_posting(self, db):
        resp = _fake_response(
            json_data=[
                _LEGAL_NOTICE,
                {
                    "id": "12345",
                    "position": "Data Engineer",
                    "company": "Acme Corp",
                    "description": "<p>Build pipelines.</p>",
                    "url": "https://remoteok.com/remote-jobs/12345",
                    "location": "Worldwide",
                    "date": "2026-09-08T14:57:50+00:00",
                },
            ]
        )
        with patch.object(remoteok_source.requests, "get", return_value=resp):
            postings = remoteok_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.job_title == "Data Engineer"
        assert posting.company_name_raw == "Acme Corp"
        assert posting.external_id == "12345"
        assert posting.job_description.strip() == "Build pipelines."
        assert posting.posted_at is not None

    def test_filters_out_non_matching_titles(self, db):
        resp = _fake_response(
            json_data=[
                _LEGAL_NOTICE,
                {"id": "1", "position": "Sales Manager", "company": "Acme", "url": "https://x"},
            ]
        )
        with patch.object(remoteok_source.requests, "get", return_value=resp):
            postings = remoteok_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_network_failure_returns_empty(self, db):
        with patch.object(remoteok_source.requests, "get", side_effect=Exception("boom")):
            assert remoteok_source.cheap_scan(["Data Engineer"], "United States") == []


class TestFetchFullDescription:
    def test_returns_already_fetched_description(self):
        posting = RawPosting(
            source="remoteok", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://remoteok.com/remote-jobs/12345", job_description="Already here.",
        )
        assert remoteok_source.fetch_full_description(posting) == "Already here."
