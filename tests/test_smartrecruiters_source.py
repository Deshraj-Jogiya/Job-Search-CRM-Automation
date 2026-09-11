from unittest.mock import patch

from app.models import Company
from app.services.sources import smartrecruiters_source
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


def _make_company(db, name="Acme Corp", slug="acme"):
    from app.services.company_utils import normalize_company_name

    company = Company(name=name, normalized_name=normalize_company_name(name), smartrecruiters_slug=slug)
    db.add(company)
    db.commit()
    db.refresh(company)
    return company


class TestIsConfigured:
    def test_false_with_no_tracked_companies(self, db):
        assert smartrecruiters_source.is_configured() is False

    def test_true_once_a_company_has_a_slug(self, db):
        _make_company(db)
        assert smartrecruiters_source.is_configured() is True


class TestCheapScan:
    def test_returns_matching_posting_with_description_left_for_lazy_fetch(self, db):
        _make_company(db, name="Acme Corp", slug="acme")
        resp = _fake_response(
            json_data={
                "content": [
                    {
                        "id": "12345",
                        "name": "Data Engineer",
                        "location": {"fullLocation": "New York, NY, United States", "city": "New York"},
                    }
                ]
            }
        )
        with patch.object(smartrecruiters_source.requests, "get", return_value=resp):
            postings = smartrecruiters_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.job_title == "Data Engineer"
        assert posting.external_id == "12345"
        assert posting.job_description is None
        assert posting.job_url == "https://jobs.smartrecruiters.com/acme/12345"

    def test_filters_out_non_matching_titles(self, db):
        _make_company(db)
        resp = _fake_response(json_data={"content": [{"id": "1", "name": "Sales Manager", "location": {}}]})
        with patch.object(smartrecruiters_source.requests, "get", return_value=resp):
            postings = smartrecruiters_source.cheap_scan(["Data Engineer"], "United States")
        assert postings == []

    def test_no_tracked_companies_returns_empty(self, db):
        assert smartrecruiters_source.cheap_scan(["Data Engineer"], "United States") == []


class TestFetchFullDescription:
    def test_fetches_and_joins_the_real_section_text(self, db):
        posting = RawPosting(
            source="smartrecruiters", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://jobs.smartrecruiters.com/acme/12345",
        )
        resp = _fake_response(
            json_data={
                "jobAd": {
                    "sections": {
                        "jobDescription": {"text": "<p>Build pipelines.</p>"},
                        "qualifications": {"text": "<p>3+ years experience.</p>"},
                        "companyDescription": {"text": "<p>We are Acme.</p>"},
                    }
                }
            }
        )
        with patch.object(smartrecruiters_source.requests, "get", return_value=resp) as mock_get:
            description = smartrecruiters_source.fetch_full_description(posting)

        assert "Build pipelines." in description
        assert "3+ years experience." in description
        assert "We are Acme." in description
        # description-first ordering
        assert description.index("Build pipelines.") < description.index("We are Acme.")
        mock_get.assert_called_once_with(
            "https://api.smartrecruiters.com/v1/companies/acme/postings/12345", timeout=10
        )

    def test_malformed_url_returns_empty_string_without_a_network_call(self, db):
        posting = RawPosting(
            source="smartrecruiters", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://example.com/not-a-smartrecruiters-url",
        )
        with patch.object(smartrecruiters_source.requests, "get") as mock_get:
            assert smartrecruiters_source.fetch_full_description(posting) == ""
        mock_get.assert_not_called()

    def test_network_failure_returns_empty_string(self, db):
        posting = RawPosting(
            source="smartrecruiters", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://jobs.smartrecruiters.com/acme/12345",
        )
        with patch.object(smartrecruiters_source.requests, "get", side_effect=Exception("boom")):
            assert smartrecruiters_source.fetch_full_description(posting) == ""
