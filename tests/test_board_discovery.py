"""Covers the Recruitee company-name cross-check added after a live
false-positive: an auto-discovered "bbc" slug on Recruitee turned out to
belong to an unrelated Belgian ad agency ("BBC NV"), not the company
actually being tracked. See app/services/board_discovery.py."""

from unittest.mock import patch

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
