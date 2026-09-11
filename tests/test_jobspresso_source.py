from unittest.mock import patch

from app.services.sources import jobspresso_source
from app.services.sources.base import RawPosting

_SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
  xmlns:content="http://purl.org/rss/1.0/modules/content/"
  xmlns:job_listing="https://jobspresso.co">
<channel>
<title>Jobspresso</title>
<item>
<title>Data Engineer</title>
<link>https://jobspresso.co/job/data-engineer/</link>
<guid>https://jobspresso.co/?post_type=job_listing&amp;p=1</guid>
<pubDate>Sat, 29 Aug 2026 02:12:12 +0000</pubDate>
<job_listing:company>Acme Corp</job_listing:company>
<job_listing:location>Worldwide</job_listing:location>
<description><![CDATA[Short excerpt.]]></description>
<content:encoded><![CDATA[<p>Build pipelines.</p>]]></content:encoded>
</item>
<item>
<title>Sales Manager</title>
<link>https://jobspresso.co/job/sales-manager/</link>
<job_listing:company>Beta Inc</job_listing:company>
<description><![CDATA[Sells things.]]></description>
</item>
</channel>
</rss>
"""


def _fake_response(content=_SAMPLE_FEED.encode("utf-8"), status_code=200):
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.content = content

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception(f"HTTP {self.status_code}")

    return _Resp()


class TestIsConfigured:
    def test_always_true(self):
        assert jobspresso_source.is_configured() is True


class TestCheapScan:
    def test_returns_matching_posting_using_content_encoded_field(self, db):
        with patch.object(jobspresso_source.requests, "get", return_value=_fake_response()):
            postings = jobspresso_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.company_name_raw == "Acme Corp"
        assert posting.job_title == "Data Engineer"
        assert posting.location == "Worldwide"
        # Full content:encoded body ("Build pipelines."), not the short <description> excerpt.
        assert posting.job_description.strip() == "Build pipelines."
        assert posting.posted_at is not None

    def test_filters_out_non_matching_titles(self, db):
        with patch.object(jobspresso_source.requests, "get", return_value=_fake_response()):
            postings = jobspresso_source.cheap_scan(["Data Engineer"], "United States")
        titles = [p.job_title for p in postings]
        assert "Sales Manager" not in titles

    def test_falls_back_to_description_when_no_content_encoded(self, db):
        with patch.object(jobspresso_source.requests, "get", return_value=_fake_response()):
            postings = jobspresso_source.cheap_scan(["Sales Manager"], "United States")
        assert len(postings) == 1
        assert postings[0].job_description.strip() == "Sells things."

    def test_network_failure_returns_empty(self, db):
        with patch.object(jobspresso_source.requests, "get", side_effect=Exception("boom")):
            assert jobspresso_source.cheap_scan(["Data Engineer"], "United States") == []


class TestFetchFullDescription:
    def test_returns_already_fetched_description(self):
        posting = RawPosting(
            source="jobspresso", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://jobspresso.co/job/1/", job_description="Already here.",
        )
        assert jobspresso_source.fetch_full_description(posting) == "Already here."
