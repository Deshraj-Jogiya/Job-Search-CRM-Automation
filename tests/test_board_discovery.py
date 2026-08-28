"""Covers the Recruitee company-name cross-check added after a live
false-positive: an auto-discovered "bbc" slug on Recruitee turned out to
belong to an unrelated Belgian ad agency ("BBC NV"), not the company
actually being tracked. The check originally read the API's own
`company_name` field, but Recruitee removed that field from
/api/offers/ entirely (confirmed live 2026-08-28 -- the response is
bare {"offers": [...]} now); the check was rebuilt to read the public
careers page's `og:site_name` meta tag instead, since `requests`
follows redirects by default and a slug whose page has moved to a
custom domain lands on THAT domain's own og:site_name -- confirmed
live that "bbc" now redirects to careers.bbc.be, whose og:site_name is
still "BBC NV", the exact same real rejection this test file is named
for. See app/services/board_discovery.py."""

from unittest.mock import patch

import pytest

from app.services import board_discovery


def _fake_response(status_code=200, json_data=None, text=""):
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.text = text

        def json(self):
            return json_data

    return _Resp()


def _og_site_name_html(name):
    return f'<meta content="{name}" property="og:site_name"/>'


def _fake_recruitee_get(offers_json, page_html, page_status=200):
    def fake_get(url, timeout):
        if "/api/offers/" in url:
            return _fake_response(json_data=offers_json)
        return _fake_response(status_code=page_status, text=page_html)

    return fake_get


def test_probe_recruitee_rejects_unrelated_company_name():
    fake_get = _fake_recruitee_get({"offers": []}, _og_site_name_html("BBC NV"))
    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
        assert board_discovery._probe_recruitee("bbc", "British Broadcasting Corporation") is False


def test_probe_recruitee_accepts_matching_company_name():
    fake_get = _fake_recruitee_get({"offers": []}, _og_site_name_html("Stripe Inc."))
    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
        assert board_discovery._probe_recruitee("stripe", "Stripe") is True


def test_probe_recruitee_rejects_missing_og_site_name():
    fake_get = _fake_recruitee_get({"offers": []}, "<html><body>no meta tags here</body></html>")
    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
        assert board_discovery._probe_recruitee("stripe", "Stripe") is False


def test_probe_recruitee_rejects_non_200_offers():
    resp = _fake_response(status_code=404)
    with patch.object(board_discovery.requests, "get", return_value=resp):
        assert board_discovery._probe_recruitee("nope", "Stripe") is False


def test_probe_recruitee_rejects_when_the_page_has_moved_off_tenant():
    # requests follows redirects by default -- a slug whose careers
    # page redirects to Recruitee's generic marketing site (no board
    # actually hosted there) lands on a 200 with no og:site_name at
    # all, same as the missing-tag case; this test covers the page
    # request itself failing (e.g. the final destination errors).
    fake_get = _fake_recruitee_get({"offers": []}, "", page_status=502)
    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
        assert board_discovery._probe_recruitee("moved-away", "Stripe") is False


def test_discover_slugs_passes_company_name_through_to_recruitee_probe():
    def fake_get(url, timeout):
        if "/api/offers/" in url:
            return _fake_response(json_data={"offers": []})
        if "recruitee.com" in url:
            return _fake_response(text=_og_site_name_html("Acme Corp"))
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
    fake_get = _fake_recruitee_get({"offers": []}, _og_site_name_html("Unrelated Company"))
    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
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
    # Lever's postings carry no company-identifying field either
    # (confirmed on the real job object, same v0 endpoint _probe_lever
    # uses) -- nothing recoverable there, unlike Greenhouse.
    assert board_discovery.fetch_verified_name("lever", "some-real-board") is None


def test_probe_lever_uses_the_still_public_v0_endpoint_not_the_auth_walled_v1():
    # v1 (with or without ?mode=json) started returning 401 for every
    # company tested on 2026-08-28; v0 is still public and returns the
    # same shape. Assert the probe actually hits v0, not v1 -- a
    # regression back to v1 would make every Lever probe silently fail.
    captured_urls = []

    def fake_get(url, timeout):
        captured_urls.append(url)
        return _fake_response(json_data=[])

    with patch.object(board_discovery.requests, "get", side_effect=fake_get):
        assert board_discovery._probe_lever("some-real-board") is True

    assert captured_urls == ["https://api.lever.co/v0/postings/some-real-board"]
