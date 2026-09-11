from unittest.mock import patch

from app.services.sources import weworkremotely_source
from app.services.sources.base import RawPosting

_SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
<title>We Work Remotely</title>
<item>
<title>Acme Corp: Data Engineer</title>
<region>Anywhere in the World</region>
<link>https://weworkremotely.com/remote-jobs/acme-corp-data-engineer</link>
<guid>https://weworkremotely.com/remote-jobs/acme-corp-data-engineer</guid>
<pubDate>Tue, 18 Aug 2026 20:32:37 +0000</pubDate>
<description><![CDATA[<p>Build pipelines.</p>]]></description>
</item>
<item>
<title>NoColonTitle</title>
<link>https://weworkremotely.com/remote-jobs/no-colon</link>
<description><![CDATA[<p>Sales Manager role.</p>]]></description>
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
        assert weworkremotely_source.is_configured() is True


class TestSplitTitle:
    def test_splits_company_and_title(self):
        assert weworkremotely_source._split_title("Acme Corp: Data Engineer") == ("Acme Corp", "Data Engineer")

    def test_no_colon_falls_back_to_unknown_company(self):
        assert weworkremotely_source._split_title("Just A Title") == ("Unknown Company", "Just A Title")


class TestCheapScan:
    def test_returns_matching_posting(self, db):
        with patch.object(weworkremotely_source.requests, "get", return_value=_fake_response()):
            postings = weworkremotely_source.cheap_scan(["Data Engineer"], "United States")

        assert len(postings) == 1
        posting = postings[0]
        assert posting.company_name_raw == "Acme Corp"
        assert posting.job_title == "Data Engineer"
        assert posting.job_description.strip() == "Build pipelines."
        assert posting.location == "Anywhere in the World"
        assert posting.posted_at is not None

    def test_filters_out_non_matching_titles(self, db):
        with patch.object(weworkremotely_source.requests, "get", return_value=_fake_response()):
            postings = weworkremotely_source.cheap_scan(["Sales Manager"], "United States")
        # "NoColonTitle" entry has no seniority/keyword match for "Sales Manager"
        # since its title is literally "NoColonTitle" -- confirms local filtering runs.
        assert postings == []

    def test_network_failure_returns_empty(self, db):
        with patch.object(weworkremotely_source.requests, "get", side_effect=Exception("boom")):
            assert weworkremotely_source.cheap_scan(["Data Engineer"], "United States") == []


class TestFetchFullDescription:
    def test_returns_already_fetched_description(self):
        posting = RawPosting(
            source="weworkremotely", company_name_raw="Acme", job_title="Data Engineer",
            job_url="https://weworkremotely.com/remote-jobs/1", job_description="Already here.",
        )
        assert weworkremotely_source.fetch_full_description(posting) == "Already here."
