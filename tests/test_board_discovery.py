"""Covers the Recruitee company-name cross-check added after a live
false-positive: an auto-discovered "bbc" slug on Recruitee turned out to
belong to an unrelated Belgian ad agency ("BBC NV"), not the company
actually being tracked. See app/services/board_discovery.py."""

from unittest.mock import patch

import pytest

from app.services import board_discovery


def _fake_response(status_code=200, json_data=None):
    class _Resp:
        def __init__(self):
            self.status_code = status_code

        def json(self):
            return json_data

    return _Resp()


def test_probe_recruitee_rejects_unrelated_company_name():
    resp = _fake_response(json_data={"offers": [], "company_name": "BBC NV"})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery._probe_recruitee("bbc", "British Broadcasting Corporation") is False


def test_probe_recruitee_accepts_matching_company_name():
    resp = _fake_response(json_data={"offers": [], "company_name": "Stripe Inc."})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery._probe_recruitee("stripe", "Stripe") is True


def test_probe_recruitee_rejects_missing_company_name():
    resp = _fake_response(json_data={"offers": []})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery._probe_recruitee("stripe", "Stripe") is False


def test_probe_recruitee_rejects_non_200():
    resp = _fake_response(status_code=404)
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery._probe_recruitee("nope", "Stripe") is False


def test_discover_slugs_passes_company_name_through_to_recruitee_probe():
    def fake_get(url, timeout):
        if "recruitee.com" in url:
            return _fake_response(json_data={"offers": [], "company_name": "Acme Corp"})
        return _fake_response(status_code=404)

    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
        result = board_discovery.discover_slugs("Acme")
    assert result["recruitee"] == "acme"
    assert result["greenhouse"] is None


def test_probe_known_slug_verifies_an_externally_sourced_slug():
    resp = _fake_response(json_data={"jobs": []})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery.probe_known_slug("greenhouse", "stripe") is True


def test_probe_known_slug_false_when_the_dataset_slug_has_gone_stale():
    resp = _fake_response(status_code=404)
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery.probe_known_slug("lever", "some-old-slug") is False


def test_probe_known_slug_passes_company_name_to_recruitee_for_its_cross_check():
    resp = _fake_response(json_data={"offers": [], "company_name": "Unrelated Company"})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery.probe_known_slug("recruitee", "bbc", "British Broadcasting Corporation") is False


def test_probe_known_slug_rejects_unknown_ats_type():
    with pytest.raises(ValueError):
        board_discovery.probe_known_slug("workday", "some-slug")


def test_fetch_verified_name_returns_the_real_greenhouse_company_name():
    resp = _fake_response(json_data={"jobs": [{"company_name": "Davidson Kempner Capital Management"}]})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery.fetch_verified_name("greenhouse", "1456754456yhgbhfg") == "Davidson Kempner Capital Management"


def test_fetch_verified_name_greenhouse_no_jobs_returns_none():
    resp = _fake_response(json_data={"jobs": []})
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery.fetch_verified_name("greenhouse", "empty-board") is None


def test_fetch_verified_name_greenhouse_non_200_returns_none():
    resp = _fake_response(status_code=404)
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery.fetch_verified_name("greenhouse", "gone") is None


def test_fetch_verified_name_ashby_has_no_name_field_so_returns_none():
    # Ashby's job-board API genuinely carries no company/organization
    # field anywhere in the response (confirmed live) -- always None,
    # regardless of what requests.get would return, since the function
    # doesn't even attempt a lookup for this platform.
    assert board_discovery.fetch_verified_name("ashby", "some-real-board") is None


def test_fetch_verified_name_lever_returns_none():
    # Lever's public postings API currently requires authentication for
    # every company (a real, confirmed outage, not company-specific) --
    # nothing recoverable there right now.
    assert board_discovery.fetch_verified_name("lever", "some-real-board") is None
