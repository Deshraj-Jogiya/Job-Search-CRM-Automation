from unittest.mock import patch

from app.services.sources import remotive_source
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
        assert remotive_source.is_configured() is True


class TestCheapScan:
    def test_makes_one_call_per_keyword(self, db):
        resp = _fake_response(json_data={"jobs": []})
        with patch.object(remotive_source.requests, "get", return_value=resp) as mock_get:
            remotive_source.cheap_scan(["Data Engineer", "ML Engineer"], "United States")
        assert mock_get.call_count == 2
        first_call_params = mock_get.call_args_list[0].kwargs["params"]
        assert first_call_params["search"] == "Data Engineer"

    def test_returns_matching_posting_with_description_cleaned(self, db):
        resp = _fake_response(
            json_data={
                "jobs": [
                    {
                        "id": 2086540,
                        "url": "https://remotive.com/remote-jobs/data/data-engineer-2086540",
                        "title": "Data Engineer",
                        "company_name": "Acme Corp",
                        "candidate_required_location": "USA Only",
                        "publication_date": "2026-09-08T21:47:54",
                        "description": "<p>Build <strong>pipelines</strong>.</p>",
                    }
                ]
            }
        )
        with patch.object(remotive_source.requests, "get", return_value=resp):
            postings = remotive_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.job_title == "Data Engineer"
        assert posting.company_name_raw == "Acme Corp"
        assert posting.external_id == "2086540"
        assert "Build" in posting.job_description and "pipelines" in posting.job_description
        assert posting.location == "USA Only"
        assert posting.posted_at is not None

    def test_skips_posting_with_no_url(self, db):
        resp = _fake_response(json_data={"jobs": [{"id": 1, "url": "", "title": "Data Engineer"}]})
        with patch.object(remotive_source.requests, "get", return_value=resp):
            postings = remotive_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_one_keyword_failure_does_not_block_others(self, db):
        good_resp = _fake_response(
            json_data={"jobs": [{"id": 1, "url": "https://x", "title": "Data Engineer", "company_name": "Acme"}]}
        )

        def fake_get(url, params, timeout):
            if params["search"] == "bad":
                raise Exception("boom")
            return good_resp

        with patch.object(remotive_source.requests, "get", side_effect=fake_get):
            postings = remotive_source.cheap_scan(["bad", "Data Engineer"], "United States")
        assert len(postings) == 1


class TestFetchFullDescription:
    def test_returns_already_fetched_description(self):
        posting = RawPosting(
            source="remotive", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://remotive.com/remote-jobs/1", job_description="Already here.",
        )
        assert remotive_source.fetch_full_description(posting) == "Already here."
